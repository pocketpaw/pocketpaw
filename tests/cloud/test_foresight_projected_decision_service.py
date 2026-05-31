# tests/cloud/test_foresight_projected_decision_service.py — RFC 08 PR 5.
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision)
# — service-level tests for the per-anchor projection fanout under
# ``ee.cloud.foresight.service.emit_projected_decision`` +
# ``list_projected_decisions``.
"""Tests for the ProjectedDecision service surface (RFC §7.7 + PR 5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightProjectedDecisionEmitted,
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
) -> CreateScenarioRequest:
    return CreateScenarioRequest(
        name=name,
        sub_type=sub_type,
        n_ticks=n_ticks,
        personas=personas
        or [
            PersonaSpecRequest(name="Anne", role="approver", ocean={}),
        ],
    )


# ---------------------------------------------------------------------------
# Per-tick emission via the engine callback
# ---------------------------------------------------------------------------


async def test_create_scenario_run_fans_per_anchor_projections(recording_bus) -> None:
    """A Decision Forecast run with n_ticks=2 produces one anchor × 2
    ticks = 2 projected-decision records."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body(name="renewal", n_ticks=2))
    assert run.status == "complete"

    listing = await foresight_service.list_projected_decisions(ctx, run.id)
    assert listing.total == 2
    assert len(listing.items) == 2
    assert all(item.run_id == run.id for item in listing.items)
    assert all(item.anchor_id == "decision:renewal" for item in listing.items)
    # Tick ids in order (the index keeps the sort stable).
    assert [item.tick_id for item in listing.items] == [0, 1]
    # Sub_type echoes back through every record.
    assert all(item.sub_type == "decision_forecast" for item in listing.items)
    # forward_precedent_decision_id is stubbed None per PR brief.
    assert all(item.forward_precedent_decision_id is None for item in listing.items)


async def test_market_sim_run_fans_per_segment_projections() -> None:
    """Market Sim with 2 segments × 2 ticks = 4 records, one per
    (segment × tick) bucket."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx,
        _body(
            name="market-q3",
            sub_type="market_sim",
            n_ticks=2,
            personas=[
                PersonaSpecRequest(name="acme", role="enterprise"),
                PersonaSpecRequest(name="globex", role="enterprise"),
                PersonaSpecRequest(name="quickserve", role="smb"),
            ],
        ),
    )
    listing = await foresight_service.list_projected_decisions(ctx, run.id)
    assert listing.total == 4  # 2 unique segments × 2 ticks
    anchors = {item.anchor_id for item in listing.items}
    assert anchors == {"segment:enterprise", "segment:smb"}


async def test_org_change_run_fans_per_rollout_event_projections() -> None:
    """Org Change with 4 ticks always fans 4 anchors × 4 ticks = 16 records."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx,
        _body(
            name="policy-rollout",
            sub_type="org_change_rehearsal",
            n_ticks=4,
            personas=[
                PersonaSpecRequest(name="m1", role="manager"),
                PersonaSpecRequest(name="ic1", role="ic"),
            ],
        ),
    )
    listing = await foresight_service.list_projected_decisions(ctx, run.id, limit=100)
    # 4 anchors × 4 ticks
    assert listing.total == 16
    anchors = {item.anchor_id for item in listing.items}
    assert anchors == {
        "rollout:announce",
        "rollout:training",
        "rollout:deadline",
        "rollout:escalation",
    }


async def test_create_scenario_emits_projected_decision_event(recording_bus) -> None:
    """The per-tick callback emits ``ForesightProjectedDecisionEmitted``
    so the Live panel can render the timeline without polling."""
    ctx = _ctx()
    await foresight_service.create_scenario_run(ctx, _body(name="event-stream", n_ticks=3))
    emitted = [e for e in recording_bus.events if isinstance(e, ForesightProjectedDecisionEmitted)]
    # Decision Forecast = 1 anchor × 3 ticks
    assert len(emitted) == 3
    assert all(e.data["anchor_id"] == "decision:event-stream" for e in emitted)


# ---------------------------------------------------------------------------
# list_projected_decisions: pagination + filter + tenancy
# ---------------------------------------------------------------------------


async def test_list_filters_by_anchor_id() -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx,
        _body(
            name="market-filter",
            sub_type="market_sim",
            n_ticks=2,
            personas=[
                PersonaSpecRequest(name="acme", role="enterprise"),
                PersonaSpecRequest(name="quickserve", role="smb"),
            ],
        ),
    )
    listing = await foresight_service.list_projected_decisions(
        ctx, run.id, anchor_id="segment:enterprise"
    )
    assert listing.total == 2
    assert all(item.anchor_id == "segment:enterprise" for item in listing.items)


async def test_list_paginates_with_offset_and_limit() -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(
        ctx,
        _body(
            name="paged",
            sub_type="org_change_rehearsal",
            n_ticks=4,
            personas=[PersonaSpecRequest(name="x", role="manager")],
        ),
    )
    page1 = await foresight_service.list_projected_decisions(ctx, run.id, limit=5)
    assert len(page1.items) == 5
    assert page1.has_more is True
    assert page1.total == 16

    page2 = await foresight_service.list_projected_decisions(ctx, run.id, limit=5, offset=5)
    assert len(page2.items) == 5
    assert page2.offset == 5

    last_page = await foresight_service.list_projected_decisions(ctx, run.id, limit=10, offset=10)
    # Only 6 records remain after offset 10 in a 16-record set.
    assert len(last_page.items) == 6
    assert last_page.has_more is False


async def test_list_unknown_run_returns_404() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.list_projected_decisions(ctx, "5f50c31b1c9d440000000000")


async def test_list_malformed_run_id_returns_404() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.list_projected_decisions(ctx, "not-an-objectid")


async def test_list_cross_tenant_run_returns_404() -> None:
    """A run created in w1 must be invisible from w2 even on the
    projection-list endpoint — same collapsing rule the get endpoint
    uses so existence isn't cross-tenant leakable."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    run = await foresight_service.create_scenario_run(ctx_w1, _body(name="tenant-isolated"))
    with pytest.raises(NotFound):
        await foresight_service.list_projected_decisions(ctx_w2, run.id)


async def test_list_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.list_projected_decisions(ctx, "0" * 24)


async def test_list_invalid_limit_raises() -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body())
    with pytest.raises(ValidationError):
        await foresight_service.list_projected_decisions(ctx, run.id, limit=0)


async def test_list_invalid_offset_raises() -> None:
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body())
    with pytest.raises(ValidationError):
        await foresight_service.list_projected_decisions(ctx, run.id, offset=-1)


async def test_list_caps_limit_at_500() -> None:
    """A request that asks for 10000 records is silently capped at 500
    so a misconfigured caller can't drag the whole collection into
    memory."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body())
    listing = await foresight_service.list_projected_decisions(ctx, run.id, limit=10000)
    assert listing.limit == 500


# ---------------------------------------------------------------------------
# emit_projected_decision — direct callable, used by the engine closure
# ---------------------------------------------------------------------------


async def test_emit_projected_decision_persists_record(recording_bus) -> None:
    """The emit function is the engine callback's target — it persists
    one document + emits one event."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _body(name="seed"))

    # Direct emit — simulates a future caller that constructs anchors
    # outside the runner loop (e.g. v1.0's calibration-loop wiring).
    record = await foresight_service.emit_projected_decision(
        workspace_id="w1",
        run_id=run.id,
        anchor_id="custom:anchor",
        persona_id="p1",
        tick_id=99,
        decision_text="accept",
        confidence=0.8,
        sub_type="decision_forecast",
    )
    assert record.anchor_id == "custom:anchor"
    assert record.tick_id == 99
    assert record.workspace_id == "w1"

    # The new record now surfaces via list (filtered by anchor).
    listing = await foresight_service.list_projected_decisions(
        ctx, run.id, anchor_id="custom:anchor"
    )
    assert listing.total == 1


async def test_emit_projected_decision_requires_workspace() -> None:
    with pytest.raises(Forbidden):
        await foresight_service.emit_projected_decision(
            workspace_id="",
            run_id="r",
            anchor_id="a",
            persona_id="",
            tick_id=0,
            decision_text="noop",
            confidence=0.0,
            sub_type="decision_forecast",
        )
