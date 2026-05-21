"""Golden-response tests for the auth router profile endpoints.

Avatar upload + serve and the fastapi-users sub-routers (login/logout/
register) are NOT golden-tested here — they integrate with fastapi-users
and the filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership


async def _seed_user() -> _UserDoc:
    doc = _UserDoc(
        email="a@b.c",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Alice",
        status="online",
        active_workspace="w1",
        workspaces=[
            WorkspaceMembership(
                workspace="w1",
                role="owner",
                joined_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ],
    )
    await doc.insert()
    return doc


@pytest_asyncio.fixture
async def app_client(mongo_db) -> tuple[AsyncClient, _UserDoc]:
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.auth.router import router

    user_doc = await _seed_user()

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: user_doc

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, user_doc


async def test_get_me_returns_dto_shape(app_client) -> None:
    client, user_doc = app_client
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "id",
        "email",
        "name",
        "image",
        "emailVerified",
        "activeWorkspace",
        "workspaces",
    }
    assert body["id"] == str(user_doc.id)
    assert body["name"] == "Alice"
    assert body["emailVerified"] is True
    assert body["activeWorkspace"] == "w1"
    assert body["workspaces"] == [{"workspace": "w1", "role": "owner"}]


async def test_patch_me_updates_full_name(app_client) -> None:
    client, user_doc = app_client
    resp = await client.patch("/api/v1/auth/me", json={"full_name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"

    refreshed = await _UserDoc.get(user_doc.id)
    assert refreshed is not None
    assert refreshed.full_name == "Renamed"


async def test_set_active_workspace(app_client) -> None:
    client, user_doc = app_client
    resp = await client.post("/api/v1/auth/set-active-workspace", json={"workspace_id": "w42"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "activeWorkspace": "w42"}

    refreshed = await _UserDoc.get(user_doc.id)
    assert refreshed is not None
    assert refreshed.active_workspace == "w42"


async def test_set_active_workspace_empty_returns_422(app_client) -> None:
    client, _ = app_client
    resp = await client.post("/api/v1/auth/set-active-workspace", json={"workspace_id": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "workspace_id.required"
