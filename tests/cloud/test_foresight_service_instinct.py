# tests/cloud/test_foresight_service_instinct.py — RFC 08 PR 8.
# Created: 2026-05-25 (feat/foresight-v08-approval-loop)
# — service-level tests for the Foresight → Instinct approval-loop
# fan-out under ``ee.cloud.foresight.service.emit_projected_decision``
# (when ``route_to_instinct=True``) + ``list_instinct_proposals_for_run``.
#
# The Instinct store is OSS-runtime SQLite; the tests swap the
# ``get_instinct_store`` singleton for a fresh ``InstinctStore`` rooted
# at a per-test temp path so the rows don't leak across tests or
# accumulate in the developer's ``~/.pocketpaw/instinct.db``.
"""Tests for the Foresight → Instinct approval-loop integration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightInstinctProposalCreated,
)
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    CreateScenarioRequest,
    PersonaSpecRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _body(
    *,
    name: str = "renewal-forecast",
    sub_type: str = "decision_forecast",
    n_ticks: int = 1,
    personas: list[PersonaSpecRequest] | None = None,
    route_to_instinct: bool = False,
) -> CreateScenarioRequest:
    return CreateScenarioRequest(
        name=name,
        sub_type=sub_type,
        n_ticks=n_ticks,
        personas=personas
        or [
            PersonaSpecRequest(name="Anne", role="approver", ocean={}),
        ],
        route_to_instinct=route_to_instinct,
    )


@pytest.fixture
def isolated_instinct_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Swap the global InstinctStore singleton for a fresh test-local one.

    The default ``get_instinct_store`` returns a process-wide singleton
    rooted at ``~/.pocketpaw/instinct.db``. For these tests we point
    it at a per-test SQLite file under ``tmp_path`` so:

      1. Rows don't leak across tests (each test gets a fresh DB).
      2. Rows don't accumulate in the developer's real DB.

    We monkey-patch BOTH ``pocketpaw.stores.get_instinct_store`` (where
    the singleton is defined) AND the binding the cloud service imports
    lazily, so every code path the service touches sees the test store.
    """
    from pocketpaw import stores as stores_mod
    from pocketpaw.instinct.store import InstinctStore

    test_store = InstinctStore(tmp_path / "instinct-test.db")

    def _factory() -> InstinctStore:
        return test_store

    monkeypatch.setattr(stores_mod, "get_instinct_store", _factory)
    return test_store


# ---------------------------------------------------------------------------
# Opt-in routing: a run with ``route_to_instinct=True`` spawns one
# Instinct row per ProjectedDecision; a run with the flag false spawns
# nothing.
# ---------------------------------------------------------------------------


async def test_opt_in_run_fans_one_instinct_row_per_projection(
    isolated_instinct_store, recording_bus
) -> None:
    """Decision Forecast with n_ticks=2 produces 2 projections and 2
    Instinct rows when ``route_to_instinct=True``."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx, _body(name="renewal", n_ticks=2, route_to_instinct=True)
    )
    assert run.status == "complete"

    listing = await foresight_service.list_instinct_proposals_for_run(ctx, run.id)
    assert listing.total == 2
    assert len(listing.items) == 2
    # Every proposal carries the foresight provenance block.
    for item in listing.items:
        assert item.foresight["run_id"] == run.id
        assert item.foresight["sub_type"] == "decision_forecast"
        assert item.pocket_id == f"foresight:run:{run.id}"
        # RFC §8 contract — evidence-only proposals.
        assert item.category == "data"


async def test_opt_out_run_creates_no_instinct_rows(isolated_instinct_store) -> None:
    """A scenario run with ``route_to_instinct=False`` (the default)
    must NOT create any Instinct rows even though projections still
    persist."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx, _body(name="silent", n_ticks=2, route_to_instinct=False)
    )
    # Projections still landed — the run is otherwise unchanged.
    projections = await foresight_service.list_projected_decisions(ctx, run.id)
    assert projections.total == 2

    # ...but no Instinct rows.
    listing = await foresight_service.list_instinct_proposals_for_run(ctx, run.id)
    assert listing.total == 0
    assert listing.items == []


async def test_opt_in_emits_proposal_created_event(isolated_instinct_store, recording_bus) -> None:
    """The Instinct fan-out emits one
    ``ForesightInstinctProposalCreated`` event per spawned row so the
    Tray UI can refresh without polling."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx, _body(name="event-test", n_ticks=3, route_to_instinct=True)
    )
    emitted = [e for e in recording_bus.events if isinstance(e, ForesightInstinctProposalCreated)]
    # Decision Forecast = 1 anchor × 3 ticks
    assert len(emitted) == 3
    assert all(e.data["run_id"] == run.id for e in emitted)
    # Every event carries the dedupe key so a downstream listener can
    # cross-reference without an extra Instinct round trip.
    assert {e.data["tick_id"] for e in emitted} == {0, 1, 2}


# ---------------------------------------------------------------------------
# Idempotence: a re-emit of the same (ws, run, tick, anchor, persona)
# bucket must NOT create a duplicate Instinct row.
# ---------------------------------------------------------------------------


async def test_repeat_emit_with_same_dedupe_key_does_not_duplicate(
    isolated_instinct_store,
) -> None:
    """Calling ``emit_projected_decision`` twice with identical args
    must produce only one Instinct row — the dedupe key is the gate."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body(name="dedupe-seed"))

    kwargs = {
        "workspace_id": "w1",
        "run_id": run.id,
        "anchor_id": "decision:dedupe-seed",
        "persona_id": "p1",
        "tick_id": 0,
        "decision_text": "accept",
        "confidence": 0.7,
        "sub_type": "decision_forecast",
        "route_to_instinct": True,
        "scenario_name": "dedupe-seed",
    }

    # First call — Instinct row created.
    await foresight_service.emit_projected_decision(**kwargs)
    after_first = await foresight_service.list_instinct_proposals_for_run(ctx, run.id)
    first_total = after_first.total
    assert first_total >= 1

    # Second call with identical args — no new Instinct row.
    await foresight_service.emit_projected_decision(**kwargs)
    after_second = await foresight_service.list_instinct_proposals_for_run(ctx, run.id)
    assert after_second.total == first_total


async def test_distinct_dedupe_keys_create_separate_rows(isolated_instinct_store) -> None:
    """Two emits that differ only on ``tick_id`` (or any other dedupe
    axis) must each land their own Instinct row."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body(name="distinct"))

    base_kwargs = {
        "workspace_id": "w1",
        "run_id": run.id,
        "anchor_id": "decision:distinct",
        "persona_id": "p1",
        "decision_text": "accept",
        "confidence": 0.7,
        "sub_type": "decision_forecast",
        "route_to_instinct": True,
        "scenario_name": "distinct",
    }

    before = await foresight_service.list_instinct_proposals_for_run(ctx, run.id)
    await foresight_service.emit_projected_decision(tick_id=10, **base_kwargs)
    await foresight_service.emit_projected_decision(tick_id=11, **base_kwargs)
    after = await foresight_service.list_instinct_proposals_for_run(ctx, run.id)
    assert after.total == before.total + 2


# ---------------------------------------------------------------------------
# Tenancy: list_instinct_proposals_for_run enforces the 404-collapse
# rule so a cross-tenant run-id probe can't leak existence even
# though the Instinct store itself is workspace-blind.
# ---------------------------------------------------------------------------


async def test_list_unknown_run_returns_404(isolated_instinct_store) -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.list_instinct_proposals_for_run(ctx, "5f50c31b1c9d440000000000")


async def test_list_cross_tenant_run_returns_404(isolated_instinct_store) -> None:
    """A run created in w1 must be invisible from w2 even on the
    instinct-proposals list endpoint."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    run = await foresight_service.create_scenario_run(
        ctx_w1, _body(name="tenant-iso", route_to_instinct=True)
    )
    with pytest.raises(NotFound):
        await foresight_service.list_instinct_proposals_for_run(ctx_w2, run.id)


async def test_list_requires_workspace(isolated_instinct_store) -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.list_instinct_proposals_for_run(ctx, "0" * 24)


async def test_list_invalid_limit_raises(isolated_instinct_store) -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body())
    with pytest.raises(ValidationError):
        await foresight_service.list_instinct_proposals_for_run(ctx, run.id, limit=0)


async def test_list_invalid_offset_raises(isolated_instinct_store) -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body())
    with pytest.raises(ValidationError):
        await foresight_service.list_instinct_proposals_for_run(ctx, run.id, offset=-1)


async def test_list_caps_limit_at_500(isolated_instinct_store) -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body())
    listing = await foresight_service.list_instinct_proposals_for_run(ctx, run.id, limit=10000)
    assert listing.limit == 500


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_list_paginates_with_offset_and_limit(isolated_instinct_store) -> None:
    """An Org Change run with 4 ticks × 4 anchors produces 16
    proposals when routed; pagination behaves like the projection-list
    endpoint."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx,
        _body(
            name="paged-org",
            sub_type="org_change_rehearsal",
            n_ticks=4,
            personas=[PersonaSpecRequest(name="x", role="manager")],
            route_to_instinct=True,
        ),
    )
    page1 = await foresight_service.list_instinct_proposals_for_run(ctx, run.id, limit=5)
    assert len(page1.items) == 5
    assert page1.has_more is True
    assert page1.total == 16

    page2 = await foresight_service.list_instinct_proposals_for_run(ctx, run.id, limit=5, offset=5)
    assert len(page2.items) == 5
    assert page2.offset == 5

    last_page = await foresight_service.list_instinct_proposals_for_run(
        ctx, run.id, limit=10, offset=10
    )
    # 6 remaining after offset 10 in a 16-record set.
    assert len(last_page.items) == 6
    assert last_page.has_more is False


# ---------------------------------------------------------------------------
# Backtests never route to Instinct, even when a future edit flips the
# scenario default — the backtest path explicitly passes
# ``route_to_instinct=False``.
# ---------------------------------------------------------------------------


async def test_backtest_does_not_fan_to_instinct(isolated_instinct_store) -> None:
    """A backtest reuses the scenario runner but must never spawn
    Instinct proposals — the historical-replay path is the trust
    unlock, not an operator decision queue."""
    from pocketpaw_ee.cloud.foresight.dto import (
        CreateBacktestRequest,
        HistoricalAnchorRequest,
    )

    ctx = _ctx()
    bt = await foresight_service.create_backtest(
        ctx,
        CreateBacktestRequest(
            name="bt-no-route",
            sub_type="decision_forecast",
            n_ticks=1,
            personas=[PersonaSpecRequest(name="x", role="approver")],
            anchors=[
                HistoricalAnchorRequest(
                    anchor_object_id="lease:LR-1",
                    actual_outcome={"verdict": "approve"},
                )
            ],
        ),
    )
    assert bt.status == "complete"

    # No way to query the backtest's Instinct rows (there are none) —
    # but we can confirm the SQLite store has zero rows under any
    # foresight pocket-id namespace.
    rows = await isolated_instinct_store.list_actions(limit=500)
    foresight_rows = [
        r for r in rows if str(getattr(r, "pocket_id", "")).startswith("foresight:run:")
    ]
    assert foresight_rows == []


# ---------------------------------------------------------------------------
# §14.4 — forward_precedent_decision_id threads through emit_projected_decision
# onto the persisted Beanie doc. PR #1235 introduced the engine-side
# DecisionGraphRef; PR 8 wires the cloud closure so the persisted doc
# carries the same id the runner writes to RunResult.projected_decisions.
# ---------------------------------------------------------------------------


async def test_emit_projected_decision_persists_forward_precedent_id() -> None:
    """When the cloud-side caller passes a non-None precedent id, the
    persisted ``ForesightProjectedDecision`` doc and the wire response
    both surface it instead of the hardcoded ``None``.

    Uses a custom anchor id that the run loop never emits so the
    direct ``emit_projected_decision`` call lands the only record
    under that anchor (the run itself fans ``decision:<name>``).
    """
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body(name="precedent-wire"))

    response = await foresight_service.emit_projected_decision(
        workspace_id="w1",
        run_id=run.id,
        anchor_id="custom:precedent-test",
        persona_id="p1",
        tick_id=42,
        decision_text="accept",
        confidence=0.8,
        sub_type="decision_forecast",
        forward_precedent_decision_id="synthetic-precedent-abc123def456",
    )
    assert response.forward_precedent_decision_id == "synthetic-precedent-abc123def456"

    # ...and the persisted doc round-trips through the list endpoint
    # filtered to the custom anchor so only the direct-emit row matches.
    listing = await foresight_service.list_projected_decisions(
        ctx, run.id, anchor_id="custom:precedent-test"
    )
    assert listing.total == 1
    assert listing.items[0].forward_precedent_decision_id == "synthetic-precedent-abc123def456"


async def test_emit_projected_decision_defaults_precedent_to_none() -> None:
    """Backwards-compat: callers that omit the kwarg still get the
    v0.1 wire shape (``forward_precedent_decision_id=None``)."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body(name="precedent-default"))

    response = await foresight_service.emit_projected_decision(
        workspace_id="w1",
        run_id=run.id,
        anchor_id="decision:precedent-default",
        persona_id="p1",
        tick_id=0,
        decision_text="accept",
        confidence=0.5,
        sub_type="decision_forecast",
    )
    assert response.forward_precedent_decision_id is None
