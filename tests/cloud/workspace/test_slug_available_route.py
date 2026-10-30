"""HTTP contract test for GET /workspaces/slug-available.

New file. Builds a tiny FastAPI app with the workspace router mounted and
the auth/license deps overridden (same shape as test_connectors_router),
then exercises the live slug-availability route over httpx against an
isolated mongomock-motor DB. The route's reason for existing is that it
must be matched *before* ``GET /{workspace_id}`` — these tests fail loudly
if that ordering ever regresses (the param route would 404/500 on the
literal "slug-available").
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.deps import current_user
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.workspace.router import router as workspace_router

pytestmark = pytest.mark.usefixtures("mongo_db")


def _no_op_license() -> None:
    return None


@pytest_asyncio.fixture
async def client(mongo_db) -> AsyncClient:  # noqa: ARG001 — fixture wires Beanie
    app = FastAPI()
    add_error_handler(app)
    app.include_router(workspace_router)
    # The route doesn't use the returned user, only the auth gate — any
    # truthy object satisfies it.
    app.dependency_overrides[current_user] = lambda: object()
    app.dependency_overrides[require_license] = _no_op_license
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_slug_available_returns_available(client: AsyncClient) -> None:
    resp = await client.get("/workspaces/slug-available", params={"slug": "fresh-one"})
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "reason": None}


async def test_slug_available_reports_taken(client: AsyncClient) -> None:
    await _WorkspaceDoc(name="Acme", slug="acme", owner="u1").insert()
    resp = await client.get("/workspaces/slug-available", params={"slug": "acme"})
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "reason": "taken"}


async def test_slug_available_reports_reserved(client: AsyncClient) -> None:
    resp = await client.get("/workspaces/slug-available", params={"slug": "admin"})
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "reason": "reserved"}


async def test_slug_available_reports_invalid_format(client: AsyncClient) -> None:
    resp = await client.get("/workspaces/slug-available", params={"slug": "Bad-Slug"})
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "reason": "invalid"}
