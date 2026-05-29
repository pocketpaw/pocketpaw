# tests/cloud/test_foresight_service.py — RFC 08 PR 7.
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — service-level
#   tests for ``ee.cloud.foresight.service``. Exercises the create →
#   persist → emit cycle, tenancy isolation, validation, and the
#   list / get read paths against the shared mongomock-motor fixture.
"""Tests for ``ee.cloud.foresight.service`` — Beanie persistence + emit.

Service-level coverage for the PR 7 cloud surface. Uses the ``mongo_db``
fixture from ``tests/cloud/conftest.py`` so writes flow through real
Beanie machinery against an isolated in-memory Mongo per test. The
``recording_bus`` autouse fixture captures emitted events so each write
can be paired against its event without standing up the WebSocket bus.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightRunCompleted,
    ForesightRunCreated,
    ForesightRunFailed,
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


def _body(name: str = "renewal-forecast", n_ticks: int = 1) -> CreateScenarioRequest:
    """Smallest legal POST body — one persona, deterministic backend."""
    return CreateScenarioRequest(
        name=name,
        sub_type="decision_forecast",
        n_ticks=n_ticks,
        personas=[
            PersonaSpecRequest(name="Anne", role="approver", ocean={}),
        ],
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_persists_run_with_tenancy(recording_bus) -> None:
    ctx = _ctx(workspace="w1", user="shawn")
    out = await foresight_service.create_scenario_run(ctx, _body(name="q3-renewal"))

    assert out.workspace_id == "w1"
    assert out.scenario_name == "q3-renewal"
    # PR 7 keeps the run synchronous, so POST returns ``complete`` (or
    # ``failed`` on engine error, exercised in its own test below).
    assert out.status == "complete"
    assert out.result is not None
    assert out.result["scenario_name"] == "q3-renewal"
    assert out.result["n_ticks"] == 1


async def test_create_emits_created_then_completed(recording_bus) -> None:
    ctx = _ctx()
    out = await foresight_service.create_scenario_run(ctx, _body())

    created = [e for e in recording_bus.events if isinstance(e, ForesightRunCreated)]
    completed = [e for e in recording_bus.events if isinstance(e, ForesightRunCompleted)]
    assert len(created) == 1
    assert len(completed) == 1
    # The created event carries the queued state; the completed event
    # carries the final result. Both reference the same run id.
    assert created[0].data["id"] == out.id
    assert created[0].data["status"] == "queued"
    assert completed[0].data["id"] == out.id
    assert completed[0].data["status"] == "complete"


async def test_create_rejects_unsupported_sub_type() -> None:
    ctx = _ctx()
    # PR 5 lifted market_sim + org_change_rehearsal into the supported
    # set; the next-up sub-type is ops_stress_test (PR 6+). The service
    # still surfaces engine validation as a 422 before opening a doc row.
    body = CreateScenarioRequest(
        name="early-ops-stress",
        sub_type="ops_stress_test",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="A", role="participant", ocean={})],
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_scenario_run(ctx, body)
    assert exc.value.code == "foresight.invalid_scenario"


async def test_create_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden) as exc:
        await foresight_service.create_scenario_run(ctx, _body())
    assert exc.value.code == "foresight.no_workspace"


async def test_create_captures_engine_failure_as_failed_status(monkeypatch, recording_bus) -> None:
    """If the engine raises mid-run, the doc lands as ``failed`` with
    an ``error`` populated, and a ``ForesightRunFailed`` event fires —
    not bubble out as a 500."""
    ctx = _ctx()

    async def _boom(_body, **_kwargs):  # PR 5 added workspace_id + run_id kwargs
        raise RuntimeError("simulated engine outage")

    # Patch the lazy engine call (the service's only side door into the
    # engine layer) to raise; the service should catch + persist the
    # failure rather than letting the exception bubble back to the
    # router as a 500.
    monkeypatch.setattr(foresight_service, "_run_engine_inline", _boom)

    out = await foresight_service.create_scenario_run(ctx, _body(name="boom-run"))

    assert out.status == "failed"
    assert out.error is not None
    assert "simulated engine outage" in out.error

    failed = [e for e in recording_bus.events if isinstance(e, ForesightRunFailed)]
    assert len(failed) == 1
    assert failed[0].data["id"] == out.id


# ---------------------------------------------------------------------------
# Get + tenancy
# ---------------------------------------------------------------------------


async def test_get_returns_same_payload() -> None:
    ctx = _ctx()
    created = await foresight_service.create_scenario_run(ctx, _body(name="echo"))

    fetched = await foresight_service.get_scenario_run(ctx, created.id)
    assert fetched.id == created.id
    assert fetched.scenario_name == "echo"
    assert fetched.result == created.result


async def test_get_404_for_unknown_id() -> None:
    ctx = _ctx()
    # Use a syntactically valid ObjectId that's not in the DB so we
    # exercise the ``find_one`` miss path rather than the malformed-id
    # branch (the latter is exercised below).
    with pytest.raises(NotFound):
        await foresight_service.get_scenario_run(ctx, "5f50c31b1c9d440000000000")


async def test_get_404_for_malformed_id() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.get_scenario_run(ctx, "not-an-objectid")


async def test_get_isolates_across_workspaces() -> None:
    """A run created in w1 must be invisible from w2 — and the surface
    must collapse "wrong workspace" into 404 so existence isn't leakable."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    created = await foresight_service.create_scenario_run(ctx_w1, _body(name="private"))

    with pytest.raises(NotFound):
        await foresight_service.get_scenario_run(ctx_w2, created.id)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_returns_newest_first() -> None:
    ctx = _ctx()
    first = await foresight_service.create_scenario_run(ctx, _body(name="run-1"))
    second = await foresight_service.create_scenario_run(ctx, _body(name="run-2"))
    third = await foresight_service.create_scenario_run(ctx, _body(name="run-3"))

    items = await foresight_service.list_scenario_runs(ctx)
    assert [i.id for i in items] == [third.id, second.id, first.id]


async def test_list_drops_result_blob_to_keep_payload_small() -> None:
    """The list-item shape omits ``result`` so dozens of runs serve
    cheaply. The detail endpoint is the source of truth for the full
    wire-dict — that contract is exercised in ``test_get_*`` above."""
    ctx = _ctx()
    await foresight_service.create_scenario_run(ctx, _body())

    items = await foresight_service.list_scenario_runs(ctx)
    assert len(items) == 1
    serialized = items[0].model_dump()
    assert "result" not in serialized
    assert "request" not in serialized


async def test_list_isolates_across_workspaces() -> None:
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    await foresight_service.create_scenario_run(ctx_w1, _body(name="w1-only"))
    await foresight_service.create_scenario_run(ctx_w2, _body(name="w2-only"))

    w1_items = await foresight_service.list_scenario_runs(ctx_w1)
    w2_items = await foresight_service.list_scenario_runs(ctx_w2)

    assert {i.scenario_name for i in w1_items} == {"w1-only"}
    assert {i.scenario_name for i in w2_items} == {"w2-only"}


async def test_list_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.list_scenario_runs(ctx)


async def test_list_rejects_invalid_limit() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError):
        await foresight_service.list_scenario_runs(ctx, limit=0)


async def test_list_offset_skips_initial_runs() -> None:
    """``offset`` is the server-side cursor used by both the agent-context
    wrapper and any future paginated UI surface. Five runs in newest-first
    order: ``skip=2, limit=2`` returns runs 3-4 (the third and fourth
    newest) — i.e. the second and third creates after the latest two
    were skipped."""
    ctx = _ctx()
    runs = []
    for i in range(5):
        runs.append(await foresight_service.create_scenario_run(ctx, _body(name=f"run-{i}")))

    page = await foresight_service.list_scenario_runs(ctx, limit=2, offset=2)
    # Newest-first: runs[4], runs[3], runs[2], runs[1], runs[0].
    # offset=2 drops the first two (runs[4], runs[3]); limit=2 returns
    # runs[2], runs[1] in that order.
    assert [item.id for item in page] == [runs[2].id, runs[1].id]


async def test_list_rejects_negative_offset() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError):
        await foresight_service.list_scenario_runs(ctx, offset=-1)
