# tests/cloud/test_foresight_projected_decision_router.py — RFC 08 PR 5.
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision)
# — HTTP-layer tests for the per-anchor projection fanout endpoint
# (``GET /api/v1/foresight/runs/{id}/projected-decisions``).
"""HTTP-layer tests for the ProjectedDecision list endpoint (PR 5)."""

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
        "name": "smoke-pd",
        "sub_type": "decision_forecast",
        "n_ticks": 2,
        "personas": [{"name": "Anne", "role": "approver", "ocean": {}}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_get_projected_decisions_after_post(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/foresight/scenarios", json=_payload(name="renewal-q3"))
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    g = await w1_client.get(f"/foresight/runs/{rid}/projected-decisions")
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["has_more"] is False
    assert len(body["items"]) == 2
    assert body["items"][0]["run_id"] == rid
    assert body["items"][0]["forward_precedent_decision_id"] is None


async def test_get_projected_decisions_filters_by_anchor(w1_client: AsyncClient) -> None:
    payload = _payload(
        name="market-q3",
        sub_type="market_sim",
        n_ticks=2,
        personas=[
            {"name": "acme", "role": "enterprise", "ocean": {}},
            {"name": "quickserve", "role": "smb", "ocean": {}},
        ],
    )
    r = await w1_client.post("/foresight/scenarios", json=payload)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    g = await w1_client.get(
        f"/foresight/runs/{rid}/projected-decisions",
        params={"anchor_id": "segment:enterprise"},
    )
    assert g.status_code == 200
    body = g.json()
    # 1 anchor × 2 ticks
    assert body["total"] == 2
    assert all(item["anchor_id"] == "segment:enterprise" for item in body["items"])


async def test_get_projected_decisions_paginates(w1_client: AsyncClient) -> None:
    payload = _payload(
        name="rollout-paged",
        sub_type="org_change_rehearsal",
        n_ticks=4,
        personas=[{"name": "x", "role": "manager", "ocean": {}}],
    )
    r = await w1_client.post("/foresight/scenarios", json=payload)
    rid = r.json()["id"]

    page1 = await w1_client.get(
        f"/foresight/runs/{rid}/projected-decisions",
        params={"limit": 5, "offset": 0},
    )
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["total"] == 16  # 4 anchors × 4 ticks
    assert len(body1["items"]) == 5
    assert body1["has_more"] is True

    page2 = await w1_client.get(
        f"/foresight/runs/{rid}/projected-decisions",
        params={"limit": 5, "offset": 10},
    )
    body2 = page2.json()
    assert body2["offset"] == 10
    assert len(body2["items"]) == 5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_get_404_for_unknown_run(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/runs/5f50c31b1c9d440000000000/projected-decisions")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "foresight_run.not_found"


async def test_get_404_for_malformed_run_id(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/runs/not-an-objectid/projected-decisions")
    assert r.status_code == 404


async def test_get_404_for_cross_tenant_run(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    """A run created in w1 must surface as 404 from w2 — existence
    isn't cross-tenant leakable on the projected-decisions endpoint
    any more than on the run-detail endpoint."""
    r = await w1_client.post("/foresight/scenarios", json=_payload(name="ws1-only"))
    rid = r.json()["id"]

    g = await w2_client.get(f"/foresight/runs/{rid}/projected-decisions")
    assert g.status_code == 404


async def test_get_403_without_workspace(no_ws_client: AsyncClient) -> None:
    r = await no_ws_client.get("/foresight/runs/5f50c31b1c9d440000000000/projected-decisions")
    assert r.status_code == 403


async def test_get_422_on_invalid_limit(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/foresight/scenarios", json=_payload())
    rid = r.json()["id"]
    g = await w1_client.get(f"/foresight/runs/{rid}/projected-decisions", params={"limit": 0})
    assert g.status_code == 422


async def test_get_422_on_invalid_offset(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/foresight/scenarios", json=_payload())
    rid = r.json()["id"]
    g = await w1_client.get(f"/foresight/runs/{rid}/projected-decisions", params={"offset": -1})
    assert g.status_code == 422
