# tests/cloud/test_foresight_live_snapshot_router.py
# Created: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# HTTP-layer tests for ``GET /api/v1/foresight/runs/{id}/live-snapshot``.
# Backs the paw-enterprise LivePanel (PR #267).
"""HTTP-layer tests for the live-snapshot endpoint."""

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


def _scenario_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "live-snapshot-router-test",
        "sub_type": "decision_forecast",
        "n_ticks": 2,
        "personas": [{"name": "Anne", "role": "approver", "ocean": {}}],
    }
    base.update(overrides)
    return base


async def test_get_live_snapshot_returns_200_with_locked_shape(w1_client: AsyncClient) -> None:
    create_resp = await w1_client.post("/foresight/scenarios", json=_scenario_payload())
    assert create_resp.status_code == 200, create_resp.text
    run_id = create_resp.json()["id"]

    resp = await w1_client.get(f"/foresight/runs/{run_id}/live-snapshot")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # PR #267 wire contract.
    assert body["run_id"] == run_id
    assert "generated_at" in body
    assert body["status"] in {"created", "running", "complete", "failed"}
    assert "tier_mix_actual" in body
    for tier in ("premium", "mid", "tail"):
        assert tier in body["tier_mix_actual"]
        assert 0.0 <= body["tier_mix_actual"][tier] <= 1.0
    assert isinstance(body["sampled_traces"], list)
    assert isinstance(body["anomalies"], list)


async def test_get_live_snapshot_404_for_unknown_run(w1_client: AsyncClient) -> None:
    resp = await w1_client.get("/foresight/runs/5f50c31b1c9d440000000000/live-snapshot")
    assert resp.status_code == 404


async def test_get_live_snapshot_404_for_cross_tenant(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    """A run created in w1 must collapse to 404 from w2 — existence
    is not cross-tenant leakable."""
    create_resp = await w1_client.post("/foresight/scenarios", json=_scenario_payload())
    run_id = create_resp.json()["id"]
    resp = await w2_client.get(f"/foresight/runs/{run_id}/live-snapshot")
    assert resp.status_code == 404


async def test_get_live_snapshot_sampled_traces_respect_10_cap(
    w1_client: AsyncClient, mongo_only
) -> None:
    from pocketpaw_ee.cloud.models.foresight_projected_decision import (
        ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
    )

    create_resp = await w1_client.post("/foresight/scenarios", json=_scenario_payload())
    run_id = create_resp.json()["id"]
    # Inject 15 projection rows so the cap kicks in.
    for i in range(15):
        doc = _ForesightProjectedDecisionDoc(
            workspace="w1",
            run_id=run_id,
            anchor_id=f"decision:row-{i}",
            persona_id="Anne",
            tick_id=100 + i,  # well past the engine's own ticks
            decision_text="accept",
            confidence=0.6,
            sub_type="decision_forecast",
        )
        await doc.insert()
    resp = await w1_client.get(f"/foresight/runs/{run_id}/live-snapshot")
    body = resp.json()
    assert len(body["sampled_traces"]) <= 10
