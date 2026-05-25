# tests/cloud/test_foresight_router.py — RFC 08 PR 7.
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — HTTP-layer tests
#   for ``ee.cloud.foresight.router``. Smokes the endpoint surface end-to-end
#   through a FastAPI app — status mapping, tenancy via context override,
#   create → get → list round-trip. Service-level assertions live in
#   ``test_foresight_service.py``; this file only covers the wiring.
"""HTTP-layer tests for ``ee/cloud/foresight/router.py``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
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

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def mongo_only(mongo_db: Any):
    """Reuse the shared mongo_db fixture without dragging in chat router."""
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
        "name": "smoke-run",
        "sub_type": "decision_forecast",
        "n_ticks": 1,
        "personas": [{"name": "Anne", "role": "approver", "ocean": {}}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


async def test_post_then_get(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/foresight/scenarios", json=_payload(name="echo-run"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_name"] == "echo-run"
    assert body["status"] == "complete"
    assert body["workspace_id"] == "w1"
    rid = body["id"]

    r2 = await w1_client.get(f"/foresight/runs/{rid}")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["id"] == rid
    assert body2["result"]["scenario_name"] == "echo-run"


async def test_list_endpoint_returns_lighter_shape(w1_client: AsyncClient) -> None:
    await w1_client.post("/foresight/scenarios", json=_payload(name="run-a"))
    await w1_client.post("/foresight/scenarios", json=_payload(name="run-b"))

    r = await w1_client.get("/foresight/runs")
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2
    # Most-recent-first ordering.
    assert items[0]["scenario_name"] == "run-b"
    # List shape drops the inline result blob.
    assert "result" not in items[0]
    assert "request" not in items[0]


async def test_list_respects_limit_query(w1_client: AsyncClient) -> None:
    for i in range(3):
        await w1_client.post("/foresight/scenarios", json=_payload(name=f"run-{i}"))

    r = await w1_client.get("/foresight/runs?limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2


async def test_get_404_on_unknown_run(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/runs/5f50c31b1c9d440000000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "foresight_run.not_found"


async def test_get_404_on_malformed_id(w1_client: AsyncClient) -> None:
    r = await w1_client.get("/foresight/runs/not-an-objectid")
    assert r.status_code == 404


async def test_post_422_on_unsupported_sub_type(w1_client: AsyncClient) -> None:
    r = await w1_client.post(
        "/foresight/scenarios",
        json=_payload(sub_type="ops_stress_test"),
    )
    # The engine-side validation surfaces as a 422 with the
    # foresight-namespaced error code.
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "foresight.invalid_scenario"


async def test_post_forbidden_without_workspace(no_ws_client: AsyncClient) -> None:
    r = await no_ws_client.post("/foresight/scenarios", json=_payload())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "foresight.no_workspace"


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_get_isolates_across_workspaces(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    r = await w1_client.post("/foresight/scenarios", json=_payload(name="w1-private"))
    rid = r.json()["id"]

    # w2 can't see w1's run — collapsed to 404 so existence isn't
    # cross-tenant leakable.
    r2 = await w2_client.get(f"/foresight/runs/{rid}")
    assert r2.status_code == 404


async def test_list_isolates_across_workspaces(
    w1_client: AsyncClient, w2_client: AsyncClient
) -> None:
    await w1_client.post("/foresight/scenarios", json=_payload(name="w1-only"))
    await w2_client.post("/foresight/scenarios", json=_payload(name="w2-only"))

    items_w1 = (await w1_client.get("/foresight/runs")).json()
    items_w2 = (await w2_client.get("/foresight/runs")).json()
    assert {i["scenario_name"] for i in items_w1} == {"w1-only"}
    assert {i["scenario_name"] for i in items_w2} == {"w2-only"}
