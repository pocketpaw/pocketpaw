# Cloud connectors router — contract tests.
# Created: 2026-05-03 — PR-1 of Phase 1 connector consolidation.
# Locks the wire shape and the workspace tenancy filter so future
# refactors can't silently change either.
#
# Pattern: tests build a tiny FastAPI app with the connectors router
# mounted, override the auth dependencies the same way conftest's
# cloud_app_client does, and exercise the routes via httpx.AsyncClient
# against an isolated mongomock-motor DB.

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.http import add_error_handler
from ee.cloud.connectors.router import router as connectors_router
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id


def _user_a() -> str:
    return "u-a"


def _user_b() -> str:
    return "u-b"


def _ws_a() -> str:
    return "ws-a"


def _ws_b() -> str:
    return "ws-b"


def _no_op_license() -> None:
    return None


def _build_app(workspace_id: str, user_id: str) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(connectors_router, prefix="/api/v1")
    app.dependency_overrides[current_user_id] = lambda: user_id
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = _no_op_license
    return app


@pytest_asyncio.fixture
async def client_a(mongo_db) -> AsyncClient:  # noqa: ARG001 — fixture wires Beanie
    app = _build_app(_ws_a(), _user_a())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def client_b(mongo_db) -> AsyncClient:  # noqa: ARG001
    app = _build_app(_ws_b(), _user_b())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /connectors — listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_registry_catalog(client_a: AsyncClient):
    """List returns every connector the registry knows about, all disabled by default."""
    resp = await client_a.get("/api/v1/cloud/connectors")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    # We don't pin the exact count (the YAML dir may grow), just that the
    # registry is non-empty and rows have the expected wire shape.
    assert len(rows) > 0
    sample = rows[0]
    for key in ("name", "display_name", "type", "icon", "status", "enabled"):
        assert key in sample
    # No connector is enabled in a fresh workspace.
    assert all(r["enabled"] is False for r in rows)
    assert all(r["status"] == "disconnected" for r in rows)


@pytest.mark.asyncio
async def test_list_reflects_enable(client_a: AsyncClient):
    """After /enable, the connector flips to enabled=true / status=connected."""
    # Pick an arbitrary connector from the registry.
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]

    enable = await client_a.post(
        f"/api/v1/cloud/connectors/{name}/enable",
        json={"scope": "workspace"},
    )
    assert enable.status_code == 200
    assert enable.json()["enabled"] is True
    assert enable.json()["status"] == "connected"

    listed = (await client_a.get("/api/v1/cloud/connectors")).json()
    row = next(r for r in listed if r["name"] == name)
    assert row["enabled"] is True
    assert row["status"] == "connected"
    assert row["scope"] == "workspace"


# ---------------------------------------------------------------------------
# Tenant isolation — workspace A's enabled connectors don't leak to B
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_isolation(client_a: AsyncClient, client_b: AsyncClient):
    """Enabling a connector in workspace A leaves workspace B untouched."""
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]

    await client_a.post(f"/api/v1/cloud/connectors/{name}/enable", json={"scope": "workspace"})

    a_rows = (await client_a.get("/api/v1/cloud/connectors")).json()
    b_rows = (await client_b.get("/api/v1/cloud/connectors")).json()

    assert next(r for r in a_rows if r["name"] == name)["enabled"] is True
    assert next(r for r in b_rows if r["name"] == name)["enabled"] is False


# ---------------------------------------------------------------------------
# /enable — validation + scope handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_unknown_connector_404(client_a: AsyncClient):
    resp = await client_a.post(
        "/api/v1/cloud/connectors/this-is-not-a-real-connector/enable",
        json={"scope": "workspace"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "connector.not_found"


@pytest.mark.asyncio
async def test_enable_pocket_scope_requires_pocket_id(client_a: AsyncClient):
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    resp = await client_a.post(
        f"/api/v1/cloud/connectors/{name}/enable",
        json={"scope": "pocket"},  # pocket_id missing
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "connector.scope_missing_pocket"


@pytest.mark.asyncio
async def test_enable_user_scope_requires_user_id(client_a: AsyncClient):
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    resp = await client_a.post(
        f"/api/v1/cloud/connectors/{name}/enable",
        json={"scope": "user"},  # user_id missing
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "connector.scope_missing_user"


# ---------------------------------------------------------------------------
# /disable — soft, idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_flips_status(client_a: AsyncClient):
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    await client_a.post(f"/api/v1/cloud/connectors/{name}/enable", json={"scope": "workspace"})
    resp = await client_a.post(f"/api/v1/cloud/connectors/{name}/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["status"] == "disconnected"


@pytest.mark.asyncio
async def test_disable_idempotent_when_not_enabled(client_a: AsyncClient):
    """Disabling a never-enabled connector returns the disconnected row, not 404."""
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    resp = await client_a.post(f"/api/v1/cloud/connectors/{name}/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# ---------------------------------------------------------------------------
# PATCH /config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_config_merges(client_a: AsyncClient):
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    await client_a.post(
        f"/api/v1/cloud/connectors/{name}/enable",
        json={"scope": "workspace", "config": {"a": 1, "b": 2}},
    )
    resp = await client_a.patch(
        f"/api/v1/cloud/connectors/{name}/config",
        json={"config": {"b": 99, "c": 3}},
    )
    assert resp.status_code == 200
    detail = (await client_a.get(f"/api/v1/cloud/connectors/{name}")).json()
    assert detail["config"] == {"a": 1, "b": 99, "c": 3}


@pytest.mark.asyncio
async def test_update_config_404_when_not_enabled(client_a: AsyncClient):
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    resp = await client_a.patch(
        f"/api/v1/cloud/connectors/{name}/config",
        json={"config": {"a": 1}},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{name} — detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_detail_includes_actions(client_a: AsyncClient):
    catalog = (await client_a.get("/api/v1/cloud/connectors")).json()
    name = catalog[0]["name"]
    resp = await client_a.get(f"/api/v1/cloud/connectors/{name}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == name
    assert isinstance(body["actions"], list)


@pytest.mark.asyncio
async def test_get_detail_404(client_a: AsyncClient):
    resp = await client_a.get("/api/v1/cloud/connectors/not-real")
    assert resp.status_code == 404
