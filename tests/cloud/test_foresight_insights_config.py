# tests/cloud/test_foresight_insights_config.py
# Created: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0.
# Service- + router-level tests for the per-workspace insights-synthesizer
# config surface. Exercises:
#   - GET without config → default view collapses to synthesizer="pattern",
#     llm_cache_ttl_seconds=300, updated_at=null.
#   - GET with config → echoes the stored choice + updated_at.
#   - PUT pattern → llm → upserts the doc, emits the event, GET reflects.
#   - PUT llm → pattern → emits the reverse event.
#   - PUT idempotent (same value) → no event emitted.
#   - PUT unknown synthesizer value → 422 from DTO Literal validation.
#   - PUT bad shape (extra field) → 422/400 from FastAPI / DTO.
#   - Tenancy 403: GET / PUT with no workspace → Forbidden.
#   - Cross-tenant isolation: an LLM toggle in w1 must NOT bleed into w2.
"""Tests for the per-workspace insights-synthesizer config endpoint."""

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
from pocketpaw_ee.cloud._core.realtime.events import ForesightInsightsConfigUpdated
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import SetForesightInsightsConfigRequest
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
# Service-level GET
# ---------------------------------------------------------------------------


async def test_get_insights_config_default_returns_pattern() -> None:
    """Fresh workspace — no config doc — collapses to the default view."""
    ctx = _ctx(workspace="fresh-config-ws")
    out = await foresight_service.get_insights_config(ctx)
    assert out.workspace_id == "fresh-config-ws"
    assert out.synthesizer == "pattern"
    assert out.llm_cache_ttl_seconds >= 1
    assert out.updated_at is None


async def test_get_insights_config_with_set_value_reports_llm() -> None:
    ctx = _ctx(workspace="llm-config-ws")
    await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="llm")
    )
    out = await foresight_service.get_insights_config(ctx)
    assert out.synthesizer == "llm"
    assert out.updated_at is not None
    assert "T" in out.updated_at


async def test_get_insights_config_requires_workspace() -> None:
    with pytest.raises(Forbidden):
        await foresight_service.get_insights_config(_ctx(workspace=None))


# ---------------------------------------------------------------------------
# Service-level PUT
# ---------------------------------------------------------------------------


async def test_set_insights_config_pattern_to_llm_emits(recording_bus) -> None:
    """First PUT (pattern → llm) emits the event with both values."""
    ctx = _ctx(workspace="pattern-to-llm-ws")
    out = await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="llm")
    )
    assert out.synthesizer == "llm"
    assert out.updated_at is not None

    events = [e for e in recording_bus.events if isinstance(e, ForesightInsightsConfigUpdated)]
    assert len(events) == 1
    payload = events[0].data
    assert payload["workspace_id"] == "pattern-to-llm-ws"
    assert payload["synthesizer"] == "llm"
    assert payload["previous_synthesizer"] == "pattern"


async def test_set_insights_config_llm_to_pattern_emits(recording_bus) -> None:
    """Reverting from llm back to pattern emits the reverse event."""
    ctx = _ctx(workspace="reset-config-ws")
    await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="llm")
    )
    recording_bus.events.clear()

    out = await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="pattern")
    )
    assert out.synthesizer == "pattern"

    events = [e for e in recording_bus.events if isinstance(e, ForesightInsightsConfigUpdated)]
    assert len(events) == 1
    payload = events[0].data
    assert payload["synthesizer"] == "pattern"
    assert payload["previous_synthesizer"] == "llm"


async def test_set_insights_config_noop_does_not_emit(recording_bus) -> None:
    """Writing the same value twice does not emit a second event."""
    ctx = _ctx(workspace="noop-config-ws")
    await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="llm")
    )
    recording_bus.events.clear()

    out = await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="llm")
    )
    assert out.synthesizer == "llm"
    events = [e for e in recording_bus.events if isinstance(e, ForesightInsightsConfigUpdated)]
    assert events == []


async def test_set_insights_config_pattern_when_already_default_is_noop(
    recording_bus,
) -> None:
    """Setting "pattern" on a fresh workspace (no doc) → no emit."""
    ctx = _ctx(workspace="already-pattern-ws")
    out = await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="pattern")
    )
    assert out.synthesizer == "pattern"
    events = [e for e in recording_bus.events if isinstance(e, ForesightInsightsConfigUpdated)]
    assert events == []


async def test_set_insights_config_isolates_across_workspaces() -> None:
    """A toggle in w1 must NOT bleed into w2."""
    ctx_w1 = _ctx(workspace="cfg-w1")
    ctx_w2 = _ctx(workspace="cfg-w2")
    await foresight_service.set_insights_config(
        ctx_w1, SetForesightInsightsConfigRequest(synthesizer="llm")
    )

    view_w1 = await foresight_service.get_insights_config(ctx_w1)
    view_w2 = await foresight_service.get_insights_config(ctx_w2)
    assert view_w1.synthesizer == "llm"
    assert view_w2.synthesizer == "pattern"


async def test_set_insights_config_requires_workspace() -> None:
    with pytest.raises(Forbidden):
        await foresight_service.set_insights_config(
            _ctx(workspace=None),
            SetForesightInsightsConfigRequest(synthesizer="llm"),
        )


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


def test_dto_rejects_unknown_synthesizer_value() -> None:
    """The Literal type guards against typos at the DTO layer."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        SetForesightInsightsConfigRequest(synthesizer="LLM")  # uppercase
    with pytest.raises(pydantic.ValidationError):
        SetForesightInsightsConfigRequest(synthesizer="ai")
    with pytest.raises(pydantic.ValidationError):
        SetForesightInsightsConfigRequest(synthesizer=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Router (HTTP) level
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


async def test_get_insights_config_endpoint_returns_default(
    http_client_w1: AsyncClient,
) -> None:
    resp = await http_client_w1.get("/foresight/workspace/insights-config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "workspace_id",
        "synthesizer",
        "llm_cache_ttl_seconds",
        "updated_at",
    }
    assert body["workspace_id"] == "w1"
    assert body["synthesizer"] == "pattern"
    assert body["llm_cache_ttl_seconds"] >= 1
    assert body["updated_at"] is None


async def test_put_insights_config_endpoint_sets_llm_and_get_reflects(
    http_client_w1: AsyncClient,
) -> None:
    put_resp = await http_client_w1.put(
        "/foresight/workspace/insights-config",
        json={"synthesizer": "llm"},
    )
    assert put_resp.status_code == 200
    payload = put_resp.json()
    assert payload["synthesizer"] == "llm"
    assert payload["updated_at"] is not None

    follow = await http_client_w1.get("/foresight/workspace/insights-config")
    assert follow.status_code == 200
    assert follow.json()["synthesizer"] == "llm"


async def test_put_insights_config_endpoint_unknown_value_422(
    http_client_w1: AsyncClient,
) -> None:
    """DTO Literal validation: 422 for unknown synthesizer value."""
    for bad in ("LLM", "ai", "gpt", "", None):
        resp = await http_client_w1.put(
            "/foresight/workspace/insights-config",
            json={"synthesizer": bad},
        )
        assert resp.status_code == 422, f"expected 422 for synthesizer={bad!r}"


async def test_put_insights_config_endpoint_extra_field_422_or_400(
    http_client_w1: AsyncClient,
) -> None:
    """Extra fields forbidden by the DTO (model_config extra='forbid')."""
    resp = await http_client_w1.put(
        "/foresight/workspace/insights-config",
        json={"synthesizer": "llm", "rogue_field": "bad"},
    )
    assert resp.status_code in (400, 422)


async def test_cross_tenant_isolation_via_http(mongo_db: Any) -> None:
    """A toggle set under w1 must not leak into w2."""
    # Set under w1.
    app_w1 = _build_app(workspace_id="w1")
    transport_w1 = ASGITransport(app=app_w1)
    async with AsyncClient(transport=transport_w1, base_url="http://test") as c1:
        resp = await c1.put(
            "/foresight/workspace/insights-config",
            json={"synthesizer": "llm"},
        )
        assert resp.status_code == 200

    # Read under w2 — should still be pattern.
    app_w2 = _build_app(workspace_id="w2")
    transport_w2 = ASGITransport(app=app_w2)
    async with AsyncClient(transport=transport_w2, base_url="http://test") as c2:
        resp = await c2.get("/foresight/workspace/insights-config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace_id"] == "w2"
        assert body["synthesizer"] == "pattern"
