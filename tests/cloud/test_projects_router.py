# test_projects_router.py — HTTP-layer tests for ee/cloud/projects/router.py.
# Created: 2026-05-16 — Mission Control backend completion. Smokes the
#   endpoint surface end-to-end through a FastAPI app: status mapping,
#   tenancy isolation, archive flow. Full service-level coverage lives in
#   test_projects_service.py; this file only covers the wiring.

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.context import RequestContext, ScopeKind, request_context
from ee.cloud._core.http import add_error_handler
from ee.cloud.license import require_license
from ee.cloud.projects.router import router as projects_router


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
    app.include_router(projects_router)

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    app.dependency_overrides[request_context] = _ctx
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


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": f"Project-{uuid.uuid4().hex[:6]}",
        "description": "",
        "color": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


async def test_create_then_get(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/projects", json=_payload(name="Q3 Launch"))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    r2 = await w1_client.get(f"/projects/{pid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["name"] == "Q3 Launch"
    assert body["workspace_id"] == "w1"
    assert body["status"] == "active"


async def test_list_returns_items(w1_client: AsyncClient) -> None:
    await w1_client.post("/projects", json=_payload(name="A"))
    await w1_client.post("/projects", json=_payload(name="B"))
    r = await w1_client.get("/projects")
    assert r.status_code == 200
    items = r.json()
    assert {p["name"] for p in items} == {"A", "B"}


async def test_patch_updates_fields(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/projects", json=_payload(name="orig"))
    pid = r.json()["id"]
    r2 = await w1_client.patch(f"/projects/{pid}", json={"name": "renamed", "color": "#FF0000"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "renamed"
    assert r2.json()["color"] == "#FF0000"


async def test_archive_marks_status(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/projects", json=_payload())
    pid = r.json()["id"]
    r2 = await w1_client.post(f"/projects/{pid}/archive")
    assert r2.status_code == 200
    assert r2.json()["status"] == "archived"


async def test_delete_returns_204(w1_client: AsyncClient) -> None:
    r = await w1_client.post("/projects", json=_payload())
    pid = r.json()["id"]
    r2 = await w1_client.delete(f"/projects/{pid}")
    assert r2.status_code == 204
    r3 = await w1_client.get(f"/projects/{pid}")
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# Tenancy isolation
# ---------------------------------------------------------------------------


async def test_other_workspace_cannot_read(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    r = await w1_client.post("/projects", json=_payload(name="w1-only"))
    pid = r.json()["id"]

    r2 = await w2_client.get(f"/projects/{pid}")
    assert r2.status_code == 404

    r3 = await w2_client.patch(f"/projects/{pid}", json={"name": "stolen"})
    assert r3.status_code == 404


async def test_list_is_workspace_scoped(w1_client: AsyncClient, w2_client: AsyncClient) -> None:
    await w1_client.post("/projects", json=_payload(name="w1-proj"))
    await w2_client.post("/projects", json=_payload(name="w2-proj"))
    r1 = await w1_client.get("/projects")
    r2 = await w2_client.get("/projects")
    assert {p["name"] for p in r1.json()} == {"w1-proj"}
    assert {p["name"] for p in r2.json()} == {"w2-proj"}


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


async def test_status_filter(w1_client: AsyncClient) -> None:
    r1 = await w1_client.post("/projects", json=_payload(name="active-one"))
    r2 = await w1_client.post("/projects", json=_payload(name="archive-me"))
    await w1_client.post(f"/projects/{r2.json()['id']}/archive")

    active = await w1_client.get("/projects", params={"status": "active"})
    archived = await w1_client.get("/projects", params={"status": "archived"})
    assert {p["name"] for p in active.json()} == {"active-one"}
    assert {p["name"] for p in archived.json()} == {"archive-me"}

    _ = r1
