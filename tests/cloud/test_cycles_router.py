"""HTTP-layer tests for ``ee/cloud/cycles/router.py``.

Smokes the endpoint surface end-to-end through a FastAPI app: status
mapping, tenancy isolation, and the upcoming-only edit rule. The full
service-level assertion suite lives in ``test_cycles_service.py``; this
file only covers the wiring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.cycles.router import router as cycles_router
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
    app.include_router(cycles_router)

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def mongo_only(mongo_db: Any):
    """Reuse the cloud mongo_db fixture without dragging in chat router."""
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
    base = {
        "name": f"Cycle-{uuid.uuid4().hex[:6]}",
        "description": "test",
        "start": "2026-05-01",
        "end": "2026-05-29",
        "status": "upcoming",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


async def test_create_then_get(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/cycles", json=_payload(name="May Wedding"))
    assert r.status_code == 200, r.text
    cid = r.json()["id"]

    r2 = await w1_client.get(f"/cycles/{cid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["name"] == "May Wedding"
    assert body["workspace_id"] == "w1"
    assert "daily" in body


async def test_list_returns_items_without_daily_series(w1_client: AsyncClient) -> None:
    await w1_client.post("/cycles", json=_payload())
    r = await w1_client.get("/cycles")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    # List response is the lighter shape — no daily field
    assert "daily" not in items[0]


async def test_patch_works_on_upcoming(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/cycles", json=_payload(name="orig"))
    cid = r.json()["id"]
    r2 = await w1_client.patch(f"/cycles/{cid}", json={"name": "renamed"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "renamed"


async def test_patch_forbidden_on_active(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/cycles", json=_payload(status="active"))
    cid = r.json()["id"]
    r2 = await w1_client.patch(f"/cycles/{cid}", json={"name": "no-go"})
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "cycle.not_upcoming"


async def test_close_returns_completed(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/cycles", json=_payload(status="active"))
    cid = r.json()["id"]
    r2 = await w1_client.post(f"/cycles/{cid}/close")
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"


async def test_items_endpoint_returns_array(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/cycles", json=_payload())
    cid = r.json()["id"]
    r2 = await w1_client.get(f"/cycles/{cid}/items")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)


# ---------------------------------------------------------------------------
# Tenancy isolation
# ---------------------------------------------------------------------------


async def test_other_workspace_cannot_read(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    r = await w1_client.post("/cycles", json=_payload(name="w1-cycle"))
    cid = r.json()["id"]

    r2 = await w2_client.get(f"/cycles/{cid}")
    assert r2.status_code == 404

    r3 = await w2_client.patch(f"/cycles/{cid}", json={"name": "stolen"})
    assert r3.status_code == 404


async def test_list_is_workspace_scoped(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    await w1_client.post("/cycles", json=_payload(name="w1-cycle"))
    await w2_client.post("/cycles", json=_payload(name="w2-cycle"))

    r1 = await w1_client.get("/cycles")
    r2 = await w2_client.get("/cycles")

    assert {c["name"] for c in r1.json()} == {"w1-cycle"}
    assert {c["name"] for c in r2.json()} == {"w2-cycle"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_create_rejects_inverted_dates(w1_client: AsyncClient) -> None:
    r = await w1_client.post(
        "/cycles",
        json=_payload(start="2026-06-01", end="2026-05-01"),
    )
    assert r.status_code in (400, 422)


async def test_no_workspace_returns_forbidden(no_ws_client: AsyncClient) -> None:
    r = await no_ws_client.get("/cycles")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "cycle.no_workspace"
