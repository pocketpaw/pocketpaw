# tests/cloud/test_foresight_backtest_router.py — RFC 08 PR 4.
# Created: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — HTTP-layer
#   tests for the PR 4 backtest + onboarding-gate endpoints. Service-level
#   coverage lives in ``test_foresight_backtest_service.py``; this file
#   only exercises the wiring.
"""HTTP-layer tests for the backtest + onboarding-gate routes."""

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


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "onboarding-backtest",
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
# POST /backtests
# ---------------------------------------------------------------------------


async def test_post_then_get_backtest(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/foresight/backtests", json=_payload(name="echo-bt"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_name"] == "echo-bt"
    assert body["status"] == "complete"
    assert body["workspace_id"] == "w1"
    assert body["threshold"] == 0.65
    assert body["gate_decision"] is not None
    btid = body["id"]

    r2 = await w1_client.get(f"/foresight/backtests/{btid}")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["id"] == btid


async def test_post_422_below_default_threshold(w1_client: AsyncClient) -> None:
    r = await w1_client.post(
        "/foresight/backtests",
        json=_payload(threshold=0.40),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "foresight.threshold_below_default"


async def test_post_accepts_threshold_tightening(w1_client: AsyncClient) -> None:
    r = await w1_client.post(
        "/foresight/backtests",
        json=_payload(threshold=0.85),
    )
    assert r.status_code == 200
    assert r.json()["threshold"] == 0.85


async def test_post_422_unsupported_sub_type(w1_client: AsyncClient) -> None:
    r = await w1_client.post(
        "/foresight/backtests",
        json=_payload(sub_type="ops_stress_test"),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "foresight.invalid_scenario"


async def test_post_403_without_workspace(no_ws_client: AsyncClient) -> None:
    r = await no_ws_client.post("/foresight/backtests", json=_payload())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "foresight.no_workspace"


# ---------------------------------------------------------------------------
# GET /backtests/{id}
# ---------------------------------------------------------------------------


async def test_get_404_unknown(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/backtests/5f50c31b1c9d440000000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "foresight_backtest.not_found"


async def test_get_404_malformed(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/backtests/not-an-objectid")
    assert r.status_code == 404


async def test_get_isolates_across_workspaces(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    r = await w1_client.post("/foresight/backtests", json=_payload(name="w1-private"))
    btid = r.json()["id"]
    r2 = await w2_client.get(f"/foresight/backtests/{btid}")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# GET /backtests
# ---------------------------------------------------------------------------


async def test_list_endpoint_returns_lighter_shape(w1_client: AsyncClient) -> None:
    await w1_client.post("/foresight/backtests", json=_payload(name="bt-a"))
    await w1_client.post("/foresight/backtests", json=_payload(name="bt-b"))

    r = await w1_client.get("/foresight/backtests")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2
    assert items[0]["scenario_name"] == "bt-b"  # newest first
    assert "result" not in items[0]
    assert "request" not in items[0]
    assert "gate_decision" in items[0]
    assert "threshold" in items[0]


async def test_list_respects_limit_query(w1_client: AsyncClient) -> None:
    for i in range(3):
        await w1_client.post("/foresight/backtests", json=_payload(name=f"bt-{i}"))
    r = await w1_client.get("/foresight/backtests?limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2


# ---------------------------------------------------------------------------
# GET /onboarding/gate
# ---------------------------------------------------------------------------


async def test_gate_no_backtest_returns_locked(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/onboarding/gate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unlocked"] is False
    assert body["reason"] == "no_backtest"
    assert body["threshold"] == 0.65
    assert body["workspace_id"] == "w1"
    assert body["last_backtest_id"] is None


async def test_gate_after_backtest_reports_either_unlocked_or_below_threshold(
    w1_client: AsyncClient,
) -> None:
    """After ANY completed backtest the gate moves off ``no_backtest``."""
    r_create = await w1_client.post("/foresight/backtests", json=_payload())
    assert r_create.status_code == 200
    btid = r_create.json()["id"]

    r = await w1_client.get("/foresight/onboarding/gate")
    assert r.status_code == 200
    body = r.json()
    assert body["last_backtest_id"] == btid
    assert body["reason"] in {"unlocked", "below_threshold"}


async def test_gate_requires_workspace(no_ws_client: AsyncClient) -> None:
    r = await no_ws_client.get("/foresight/onboarding/gate")
    assert r.status_code == 403
