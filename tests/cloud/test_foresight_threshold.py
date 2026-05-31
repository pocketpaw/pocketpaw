# tests/cloud/test_foresight_threshold.py — RFC 08 v1.0 PR 10.
# Created: 2026-05-26 (feat/foresight-v10-threshold-override-cloud).
# Service- + router-level tests for the per-workspace onboarding-gate
# threshold override surface. Exercises:
#   - GET without override → default view collapses to 0.65 / not
#     overridden / updated_at=null
#   - GET with override → echoes the override value + updated_at
#   - PUT (set valid) → upserts the doc, emits the event, GET reflects
#   - PUT (set null) → resets the override but keeps the doc (audit
#     trail), emits the event
#   - PUT idempotent (same value) → no event emitted
#   - PUT out-of-bounds (< 0.5 or > 0.95) → 422 from DTO validation
#   - PUT bad shape → 422 from FastAPI
#   - Cross-tenant isolation: an override in w1 must NOT bleed into w2
#   - Tenancy 403: GET / PUT with no workspace → Forbidden
"""Tests for ``ee.cloud.foresight.service`` threshold override API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import (
    RequestContext,
    ScopeKind,
    loopback_or_request_context,
)
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud._core.realtime.events import ForesightThresholdUpdated
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import SetForesightThresholdRequest
from pocketpaw_ee.cloud.foresight.router import router as foresight_router
from pocketpaw_ee.cloud.license import require_license

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Service-level: get_threshold
# ---------------------------------------------------------------------------


async def test_get_threshold_no_override_returns_default() -> None:
    """Fresh workspace — no config doc — collapses to the default view."""
    ctx = _ctx(workspace="fresh-ws")
    out = await foresight_service.get_threshold(ctx)
    assert out.workspace_id == "fresh-ws"
    assert out.current_threshold == pytest.approx(0.65)
    assert out.default_threshold == pytest.approx(0.65)
    assert out.is_overridden is False
    assert out.updated_at is None


async def test_get_threshold_with_override_reports_override() -> None:
    ctx = _ctx(workspace="tight-ws")
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.80))
    out = await foresight_service.get_threshold(ctx)
    assert out.workspace_id == "tight-ws"
    assert out.current_threshold == pytest.approx(0.80)
    assert out.default_threshold == pytest.approx(0.65)
    assert out.is_overridden is True
    assert out.updated_at is not None
    # ISO-8601 surface — string with timezone info
    assert "T" in out.updated_at


async def test_get_threshold_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.get_threshold(ctx)


# ---------------------------------------------------------------------------
# Service-level: set_threshold
# ---------------------------------------------------------------------------


async def test_set_threshold_creates_doc_and_emits(recording_bus) -> None:
    ctx = _ctx(workspace="set-ws")
    out = await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.75))
    assert out.current_threshold == pytest.approx(0.75)
    assert out.is_overridden is True
    assert out.updated_at is not None

    # Event fires on the create transition.
    events = [e for e in recording_bus.events if isinstance(e, ForesightThresholdUpdated)]
    assert len(events) == 1
    payload = events[0].data
    assert payload["workspace_id"] == "set-ws"
    assert payload["threshold"] == pytest.approx(0.75)
    assert payload["is_overridden"] is True
    assert payload["previous_threshold"] == pytest.approx(0.65)
    assert payload["previous_is_overridden"] is False


async def test_set_threshold_update_emits_with_previous(recording_bus) -> None:
    ctx = _ctx(workspace="update-ws")
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.70))
    # Drain the first event so we only assert on the second transition.
    recording_bus.events.clear()
    out = await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.85))
    assert out.current_threshold == pytest.approx(0.85)

    events = [e for e in recording_bus.events if isinstance(e, ForesightThresholdUpdated)]
    assert len(events) == 1
    payload = events[0].data
    assert payload["threshold"] == pytest.approx(0.85)
    assert payload["previous_threshold"] == pytest.approx(0.70)
    assert payload["previous_is_overridden"] is True


async def test_set_threshold_null_resets_to_default(recording_bus) -> None:
    ctx = _ctx(workspace="reset-ws")
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.80))
    recording_bus.events.clear()

    out = await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=None))
    assert out.current_threshold == pytest.approx(0.65)
    assert out.is_overridden is False
    assert out.updated_at is None  # reset → no meaningful "override updated" timestamp

    # GET right after PUT reflects the reset.
    follow_up = await foresight_service.get_threshold(ctx)
    assert follow_up.is_overridden is False
    assert follow_up.current_threshold == pytest.approx(0.65)

    # Event fires for the reset.
    events = [e for e in recording_bus.events if isinstance(e, ForesightThresholdUpdated)]
    assert len(events) == 1
    payload = events[0].data
    assert payload["threshold"] == pytest.approx(0.65)
    assert payload["is_overridden"] is False
    assert payload["previous_is_overridden"] is True


async def test_set_threshold_noop_does_not_emit(recording_bus) -> None:
    """Writing the same value twice should not emit a second event.

    Keeps the UI's optimistic local state from rebroadcasting redundant
    updates.
    """
    ctx = _ctx(workspace="noop-ws")
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.72))
    recording_bus.events.clear()

    out = await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.72))
    assert out.current_threshold == pytest.approx(0.72)

    events = [e for e in recording_bus.events if isinstance(e, ForesightThresholdUpdated)]
    assert events == []


async def test_set_threshold_null_when_already_default_is_noop(recording_bus) -> None:
    """Reset on a fresh workspace (no doc) should be a no-op emit-wise."""
    ctx = _ctx(workspace="already-default-ws")
    out = await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=None))
    assert out.is_overridden is False
    assert out.current_threshold == pytest.approx(0.65)

    events = [e for e in recording_bus.events if isinstance(e, ForesightThresholdUpdated)]
    assert events == []


async def test_set_threshold_isolates_across_workspaces() -> None:
    """An override in w1 must NOT bleed into w2."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    await foresight_service.set_threshold(ctx_w1, SetForesightThresholdRequest(threshold=0.85))

    view_w1 = await foresight_service.get_threshold(ctx_w1)
    view_w2 = await foresight_service.get_threshold(ctx_w2)
    assert view_w1.current_threshold == pytest.approx(0.85)
    assert view_w1.is_overridden is True
    assert view_w2.current_threshold == pytest.approx(0.65)
    assert view_w2.is_overridden is False


async def test_set_threshold_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.70))


# ---------------------------------------------------------------------------
# Service-level: gate + backtest wire-through (the load-bearing change)
# ---------------------------------------------------------------------------


async def test_onboarding_gate_uses_workspace_threshold() -> None:
    """``get_onboarding_gate`` must echo the workspace's effective threshold."""
    ctx = _ctx(workspace="gate-ws")
    # No override yet — gate reads the default.
    gate = await foresight_service.get_onboarding_gate(ctx)
    assert gate.threshold == pytest.approx(0.65)

    # Set override; gate now reads the override.
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.78))
    gate = await foresight_service.get_onboarding_gate(ctx)
    assert gate.threshold == pytest.approx(0.78)

    # Reset; gate falls back to default.
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=None))
    gate = await foresight_service.get_onboarding_gate(ctx)
    assert gate.threshold == pytest.approx(0.65)


async def test_create_backtest_uses_workspace_override(monkeypatch) -> None:
    """The per-run threshold is resolved against the workspace floor.

    A workspace that has tightened the floor to 0.80 must reject a
    per-run request below 0.80 even though the global default is 0.65.
    """
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.foresight.dto import (
        CreateBacktestRequest,
        HistoricalAnchorRequest,
        PersonaSpecRequest,
    )

    ctx = _ctx(workspace="strict-ws")
    await foresight_service.set_threshold(ctx, SetForesightThresholdRequest(threshold=0.80))

    body = CreateBacktestRequest(
        name="bt",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="A", role="approver", ocean={})],
        anchors=[
            HistoricalAnchorRequest(
                anchor_object_id="lease:1",
                actual_outcome={"outcome": "accept"},
            )
        ],
        threshold=0.70,  # above the global default 0.65 but below the workspace 0.80
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_backtest(ctx, body)
    assert "0.8" in str(exc.value.message)

    # A passing request — at or above 0.80 — is accepted; we stub the
    # scorer so the engine path stays out of the way (this PR's focus
    # is the threshold-resolution wiring).
    async def _passing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.9, "n_pairs": 10},
            {
                "passed": True,
                "observed": 0.9,
                "threshold": threshold,
                "margin": 0.1,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _passing_scorer)
    body_ok = body.model_copy(update={"threshold": 0.85})
    out = await foresight_service.create_backtest(ctx, body_ok)
    assert out.threshold == pytest.approx(0.85)
    # gate_decision threshold should also reflect the effective value the
    # backtest scored against — Cloud-side wire-through.
    assert out.gate_decision is not None
    assert out.gate_decision.threshold == pytest.approx(0.85)


async def test_create_backtest_defaults_to_workspace_threshold(monkeypatch) -> None:
    """When the body omits ``threshold``, the run uses the workspace floor.

    Backward compat: a workspace with no override still defaults to 0.65.
    """
    from pocketpaw_ee.cloud.foresight.dto import (
        CreateBacktestRequest,
        HistoricalAnchorRequest,
        PersonaSpecRequest,
    )

    async def _passing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.9, "n_pairs": 10},
            {
                "passed": True,
                "observed": 0.9,
                "threshold": threshold,
                "margin": threshold,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _passing_scorer)

    ctx_default = _ctx(workspace="default-ws")
    body = CreateBacktestRequest(
        name="bt",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="A", role="approver", ocean={})],
        anchors=[
            HistoricalAnchorRequest(
                anchor_object_id="lease:1",
                actual_outcome={"outcome": "accept"},
            )
        ],
    )
    out_default = await foresight_service.create_backtest(ctx_default, body)
    assert out_default.threshold == pytest.approx(0.65)

    ctx_override = _ctx(workspace="override-ws")
    await foresight_service.set_threshold(
        ctx_override, SetForesightThresholdRequest(threshold=0.75)
    )
    out_override = await foresight_service.create_backtest(ctx_override, body)
    assert out_override.threshold == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Router-level: HTTP wiring
# ---------------------------------------------------------------------------


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(foresight_router)

    async def _ctx_dep() -> RequestContext:
        return _ctx(workspace_id, user_id)

    app.dependency_overrides[loopback_or_request_context] = _ctx_dep
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def http_client_w1(mongo_db: Any) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_get_threshold_endpoint_returns_default(
    http_client_w1: AsyncClient,
) -> None:
    resp = await http_client_w1.get("/foresight/workspace/threshold")
    assert resp.status_code == 200
    body = resp.json()
    # Contract field set must match what paw-enterprise Team A2 builds against.
    assert set(body.keys()) == {
        "workspace_id",
        "current_threshold",
        "default_threshold",
        "is_overridden",
        "updated_at",
    }
    assert body["workspace_id"] == "w1"
    assert body["current_threshold"] == pytest.approx(0.65)
    assert body["default_threshold"] == pytest.approx(0.65)
    assert body["is_overridden"] is False
    assert body["updated_at"] is None


async def test_put_threshold_endpoint_sets_and_get_reflects(
    http_client_w1: AsyncClient,
) -> None:
    put_resp = await http_client_w1.put(
        "/foresight/workspace/threshold",
        json={"threshold": 0.82},
    )
    assert put_resp.status_code == 200
    payload = put_resp.json()
    assert payload["current_threshold"] == pytest.approx(0.82)
    assert payload["is_overridden"] is True
    assert payload["updated_at"] is not None

    follow = await http_client_w1.get("/foresight/workspace/threshold")
    assert follow.status_code == 200
    assert follow.json()["current_threshold"] == pytest.approx(0.82)


async def test_put_threshold_endpoint_reset_with_null(
    http_client_w1: AsyncClient,
) -> None:
    # Seed an override first.
    await http_client_w1.put("/foresight/workspace/threshold", json={"threshold": 0.80})
    resp = await http_client_w1.put("/foresight/workspace/threshold", json={"threshold": None})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["is_overridden"] is False
    assert payload["current_threshold"] == pytest.approx(0.65)
    assert payload["updated_at"] is None


async def test_put_threshold_endpoint_out_of_bounds_422(
    http_client_w1: AsyncClient,
) -> None:
    """DTO-level bounds validation: 422 with a clear error code."""
    for bad in (0.49, 0.96, -1.0, 1.5):
        resp = await http_client_w1.put("/foresight/workspace/threshold", json={"threshold": bad})
        assert resp.status_code == 422, f"expected 422 for threshold={bad}, got {resp.status_code}"


async def test_put_threshold_endpoint_malformed_body_400_or_422(
    http_client_w1: AsyncClient,
) -> None:
    """Extra fields are forbidden by the DTO; FastAPI surfaces 422.

    Different malformed shapes can surface as 400 or 422 depending on
    the parsing stage; both are valid HTTP error codes for the contract.
    """
    resp = await http_client_w1.put(
        "/foresight/workspace/threshold",
        json={"threshold": 0.7, "unknown_field": "bad"},
    )
    assert resp.status_code in (400, 422)


async def test_cross_tenant_isolation_via_http(mongo_db: Any) -> None:
    """An override set under w1 must not leak into w2."""
    # Set under w1.
    app_w1 = _build_app(workspace_id="w1")
    transport_w1 = ASGITransport(app=app_w1)
    async with AsyncClient(transport=transport_w1, base_url="http://test") as c1:
        resp = await c1.put("/foresight/workspace/threshold", json={"threshold": 0.88})
        assert resp.status_code == 200

    # Read under w2.
    app_w2 = _build_app(workspace_id="w2")
    transport_w2 = ASGITransport(app=app_w2)
    async with AsyncClient(transport=transport_w2, base_url="http://test") as c2:
        resp = await c2.get("/foresight/workspace/threshold")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_id"] == "w2"
        assert body["is_overridden"] is False
        assert body["current_threshold"] == pytest.approx(0.65)
