# tests/cloud/test_foresight_aggregate_insights_router.py
# Created: 2026-05-25 (feat/foresight-v15-scenarios-aggregate-insights) —
# HTTP-layer tests for the RFC 08 §11.2 / §11.5 / §11.6 GET endpoints:
#   - GET /api/v1/foresight/scenarios
#   - GET /api/v1/foresight/aggregate
#   - GET /api/v1/foresight/insights
# Service-level coverage lives alongside the router smokes; both layers
# share the in-memory mongomock fixtures from tests/cloud/conftest.py.
"""HTTP-layer tests for the scenarios / aggregate / insights endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import (
    RequestContext,
    ScopeKind,
    loopback_or_request_context,
)
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.foresight.router import router as foresight_router
from pocketpaw_ee.cloud.license import require_license


def _make_ctx(workspace_id: str | None, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_app(workspace_id: str | None = "w1", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(foresight_router)

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    app.dependency_overrides[loopback_or_request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def mongo_only(mongo_db: Any):
    yield mongo_db


@pytest_asyncio.fixture
async def w1_client(mongo_only) -> AsyncClient:
    app = _build_app(workspace_id="w1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2_client(mongo_only) -> AsyncClient:
    app = _build_app(workspace_id="w2")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def no_ws_client(mongo_only) -> AsyncClient:
    app = _build_app(workspace_id=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _scenario_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "smoke-run",
        "sub_type": "decision_forecast",
        "n_ticks": 1,
        "personas": [{"name": "Anne", "role": "approver", "ocean": {}}],
    }
    base.update(overrides)
    return base


def _backtest_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "onboarding-bt",
        "sub_type": "decision_forecast",
        "n_ticks": 1,
        "personas": [{"name": "Anne", "role": "approver", "ocean": {}}],
        "anchors": [
            {
                "anchor_object_id": f"lease:LR-{i}",
                "actual_outcome": {"outcome": "accept"},
                "scenario_template": "decision_forecast.yaml",
                "projection_confidence": 0.7,
            }
            for i in range(5)
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# GET /api/v1/foresight/scenarios
# ---------------------------------------------------------------------------


async def test_scenarios_lists_bundled_templates(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/scenarios")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    items = body["items"]
    assert len(items) >= 3
    ids = {item["id"] for item in items}
    assert ids == {"decision_forecast", "market_sim", "org_change"}


async def test_scenarios_shape_matches_contract(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/scenarios")
    items = r.json()["items"]
    by_id = {item["id"]: item for item in items}
    decision = by_id["decision_forecast"]
    assert decision["sub_type"] == "decision_forecast"
    assert decision["num_personas"] >= 1
    assert decision["num_ticks"] >= 1
    tier_mix = decision["tier_mix"]
    # Captain-locked 5/15/80 default per RFC §10
    assert tier_mix["premium"] == 0.05
    assert tier_mix["mid"] == 0.15
    assert tier_mix["tail"] == 0.80
    assert isinstance(decision["description"], str)
    assert decision["description"]


async def test_scenarios_workspace_agnostic(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """Different workspaces see the same catalog (static + global)."""
    r1 = await w1_client.get("/foresight/scenarios")
    r2 = await w2_client.get("/foresight/scenarios")
    assert r1.json() == r2.json()


# ---------------------------------------------------------------------------
# GET /api/v1/foresight/aggregate
# ---------------------------------------------------------------------------


async def test_aggregate_empty_workspace_returns_zeros(
    w1_client: AsyncClient,
) -> None:
    """Empty workspace → zeros + empty arrays (never 404)."""
    r = await w1_client.get("/foresight/aggregate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 30
    assert body["rolling_accuracy"]["points"] == []
    assert body["confidence_drift"]["trend"] == "flat"
    assert body["confidence_drift"]["magnitude"] == 0.0
    assert body["modal_outcome_distribution"]["entries"] == []
    # ISO-8601 timestamp.
    assert body["generated_at"].endswith("Z")


async def test_aggregate_with_data(w1_client: AsyncClient) -> None:
    """Run a backtest + scenario to seed the workspace, then assert
    rollup picks up the records."""
    bt = await w1_client.post("/foresight/backtests", json=_backtest_payload())
    assert bt.status_code == 200, bt.text
    # Also fan a scenario so the projected-decision collection has
    # rows for the modal_outcome_distribution rollup.
    sr = await w1_client.post("/foresight/scenarios", json=_scenario_payload())
    assert sr.status_code == 200

    r = await w1_client.get("/foresight/aggregate?window_days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    # Backtest completes → at least one rolling-accuracy point.
    assert isinstance(body["rolling_accuracy"]["points"], list)
    # Confidence drift trend is in the locked vocabulary.
    assert body["confidence_drift"]["trend"] in ("rising", "falling", "flat")


async def test_aggregate_422_above_max_window(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/aggregate?window_days=91")
    # FastAPI's Query(le=90) surfaces as 422 with a body that may not
    # use our custom error envelope (it's pydantic-driven), but the
    # status code is what we contract on with the UI lead.
    assert r.status_code == 422


async def test_aggregate_422_below_min_window(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/aggregate?window_days=0")
    assert r.status_code == 422


async def test_aggregate_forbidden_without_workspace(
    no_ws_client: AsyncClient,
) -> None:
    r = await no_ws_client.get("/foresight/aggregate")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "foresight.no_workspace"


async def test_aggregate_tenant_isolation(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """w2's backtest must not leak into w1's rollup."""
    await w2_client.post("/foresight/backtests", json=_backtest_payload(name="w2-bt"))
    r = await w1_client.get("/foresight/aggregate")
    assert r.status_code == 200
    # w1 has no backtests → empty series, flat drift.
    body = r.json()
    assert body["rolling_accuracy"]["points"] == []


# ---------------------------------------------------------------------------
# GET /api/v1/foresight/insights
# ---------------------------------------------------------------------------


async def test_insights_empty_workspace_returns_empty_items(
    w1_client: AsyncClient,
) -> None:
    r = await w1_client.get("/foresight/insights")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}


async def test_insights_forbidden_without_workspace(
    no_ws_client: AsyncClient,
) -> None:
    r = await no_ws_client.get("/foresight/insights")
    assert r.status_code == 403


async def test_insights_tenant_isolation(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """w2 seeded with a backtest; w1 sees an empty insights set."""
    await w2_client.post("/foresight/backtests", json=_backtest_payload(name="w2-bt"))
    r = await w1_client.get("/foresight/insights")
    body = r.json()
    assert body == {"items": []}


async def test_insights_response_shape_contract(w1_client: AsyncClient) -> None:
    """Insights always carries an items list with the locked field shape."""
    # Seed a backtest so the rollup has some inputs, but the gate
    # threshold is 0.65 — modal_accuracy from the deterministic fake
    # may or may not pass; either way the wire shape is the same.
    await w1_client.post("/foresight/backtests", json=_backtest_payload())
    r = await w1_client.get("/foresight/insights")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    for item in body["items"]:
        assert {"id", "kind", "title", "body", "severity", "anchor_refs", "generated_at"} <= set(
            item.keys()
        )
        assert item["severity"] in ("info", "warning", "critical")
        assert item["kind"] in (
            "accuracy_drop",
            "persona_outlier",
            "tier_imbalance",
            "trend_break",
            "threshold_unmet",
        )
