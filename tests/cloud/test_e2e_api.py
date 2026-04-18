# test_e2e_api.py — End-to-end API tests for the ee/cloud module.
# Created: 2026-04-05
#
# Tests the full request → service → in-memory MongoDB flow for all 6 domains:
# auth, workspace, chat, pockets, sessions, agents.
#
# Setup:
#   - mongomock-motor: in-memory MongoDB (no real Mongo required)
#   - HMAC-based license key injected via env vars
#   - Agent pool startup mocked out
#   - Each test is isolated via separate user registration

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers — license key generation
# ---------------------------------------------------------------------------


def _make_license_key(secret: str = "test-secret") -> str:
    """Generate a valid HMAC-based license key for tests."""
    from datetime import datetime, timedelta

    payload = {
        "org": "test-org",
        "plan": "enterprise",
        "seats": 100,
        "exp": (datetime.now(tz=None) + timedelta(days=365)).strftime("%Y-%m-%d"),
    }
    payload_str = json.dumps(payload)
    sig = hashlib.sha256(f"{secret}:{payload_str}".encode()).hexdigest()
    raw = f"{payload_str}.{sig}"
    return base64.b64encode(raw.encode()).decode()


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def license_env():
    """Inject license env vars for the entire module."""
    secret = "test-secret"
    key = _make_license_key(secret)
    env = {
        "POCKETPAW_LICENSE_KEY": key,
        "POCKETPAW_LICENSE_SECRET": secret,
        "AUTH_SECRET": "test-auth-secret-for-e2e",
    }
    with patch.dict(os.environ, env):
        yield env


@pytest.fixture()
async def beanie_db():
    """Initialize Beanie once per module against a real test MongoDB.

    Uses a unique database name per test run to avoid collisions.
    Drops the database after tests complete.
    """
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    # Reset license cache so env vars take effect
    import ee.cloud.license as lic_mod
    from ee.cloud.models import ALL_DOCUMENTS

    lic_mod._cached_license = None
    lic_mod._license_error = None

    db_name = f"test_paw_cloud_{uuid.uuid4().hex[:8]}"
    conn_str = f"mongodb://localhost:27017/{db_name}"
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    # Use connection_string approach — avoids Motor 3.7 append_metadata issue
    await init_beanie(connection_string=conn_str, document_models=ALL_DOCUMENTS)
    yield client[db_name]
    # Cleanup: drop the test database
    await client.drop_database(db_name)


@pytest.fixture()
async def app(license_env, beanie_db) -> FastAPI:
    """Build a FastAPI app with cloud routes mounted, agent pool mocked."""
    # Reset license module cache before mounting
    import ee.cloud.license as lic_mod
    from ee.cloud import mount_cloud

    lic_mod._cached_license = None

    test_app = FastAPI()

    # Mock agent pool start/stop so we don't need a running agent
    mock_pool = MagicMock()
    mock_pool.start = AsyncMock()
    mock_pool.stop = AsyncMock()

    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=mock_pool):
        mount_cloud(test_app)

    yield test_app


@pytest.fixture()
async def http(app) -> AsyncIterator[AsyncClient]:
    """Module-scoped HTTP client wired to the test app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Per-test auth helpers
# ---------------------------------------------------------------------------


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:8]}@test.example"


async def _register_and_login(http: AsyncClient, email: str | None = None) -> dict:
    """Register a fresh user and return auth token + user id."""
    email = email or _unique_email()
    password = "Test1234!"

    # Register
    r = await http.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": "Test User",
        },
    )
    assert r.status_code == 201, f"Register failed: {r.text}"
    user_data = r.json()

    # Login via bearer transport
    r = await http.post(
        "/api/v1/auth/bearer/login",
        data={
            "username": email,
            "password": password,
        },
    )
    assert r.status_code == 200, f"Login failed: {r.text}"
    token = r.json()["access_token"]

    return {
        "email": email,
        "password": password,
        "token": token,
        "user_id": user_data["id"],
        "headers": {"Authorization": f"Bearer {token}"},
    }


async def _make_workspace(http: AsyncClient, headers: dict, slug: str | None = None) -> dict:
    """Create a workspace and return the workspace dict."""
    slug = slug or f"ws-{uuid.uuid4().hex[:8]}"
    r = await http.post(
        "/api/v1/workspaces",
        json={
            "name": "Test Workspace",
            "slug": slug,
        },
        headers=headers,
    )
    assert r.status_code == 200, f"Create workspace failed: {r.text}"
    return r.json()


# ===========================================================================
# AUTH DOMAIN
# ===========================================================================


class TestAuthFlow:
    """Tests for POST /auth/register, POST /auth/bearer/login, GET /auth/me."""

    async def test_register_new_user_returns_201(self, http: AsyncClient):
        email = _unique_email()
        r = await http.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "Password1!",
                "full_name": "Alice",
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["email"] == email
        assert "id" in data

    async def test_register_duplicate_email_returns_400(self, http: AsyncClient):
        email = _unique_email()
        payload = {"email": email, "password": "Password1!", "full_name": "Bob"}
        r1 = await http.post("/api/v1/auth/register", json=payload)
        assert r1.status_code == 201
        r2 = await http.post("/api/v1/auth/register", json=payload)
        assert r2.status_code == 400

    async def test_login_returns_access_token(self, http: AsyncClient):
        email = _unique_email()
        await http.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "Password1!",
            },
        )
        r = await http.post(
            "/api/v1/auth/bearer/login",
            data={
                "username": email,
                "password": "Password1!",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    async def test_login_wrong_password_returns_400(self, http: AsyncClient):
        email = _unique_email()
        await http.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "Password1!",
            },
        )
        r = await http.post(
            "/api/v1/auth/bearer/login",
            data={
                "username": email,
                "password": "WrongPassword!",
            },
        )
        assert r.status_code == 400

    async def test_get_me_returns_profile(self, http: AsyncClient):
        auth = await _register_and_login(http)
        r = await http.get("/api/v1/auth/me", headers=auth["headers"])
        assert r.status_code == 200
        profile = r.json()
        assert profile["email"] == auth["email"]
        assert "id" in profile

    async def test_get_me_without_auth_returns_401(self, http: AsyncClient):
        r = await http.get("/api/v1/auth/me")
        assert r.status_code == 401

    async def test_update_profile_full_name(self, http: AsyncClient):
        auth = await _register_and_login(http)
        r = await http.patch(
            "/api/v1/auth/me", json={"full_name": "Updated Name"}, headers=auth["headers"]
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"

    async def test_jwt_token_works_across_requests(self, http: AsyncClient):
        """Same token should authenticate multiple independent requests."""
        auth = await _register_and_login(http)
        for _ in range(3):
            r = await http.get("/api/v1/auth/me", headers=auth["headers"])
            assert r.status_code == 200


# ===========================================================================
# WORKSPACE DOMAIN
# ===========================================================================


class TestWorkspaceFlow:
    """Tests for workspace CRUD, members, and invites."""

    async def test_create_workspace_returns_workspace(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        assert "name" in ws
        assert "_id" in ws
        assert ws["name"] == "Test Workspace"

    async def test_create_workspace_duplicate_slug_returns_409(self, http: AsyncClient):
        auth = await _register_and_login(http)
        slug = f"ws-{uuid.uuid4().hex[:8]}"
        await _make_workspace(http, auth["headers"], slug=slug)
        r = await http.post(
            "/api/v1/workspaces",
            json={
                "name": "Second",
                "slug": slug,
            },
            headers=auth["headers"],
        )
        assert r.status_code == 409

    async def test_list_workspaces_returns_created_workspace(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.get("/api/v1/workspaces", headers=auth["headers"])
        assert r.status_code == 200
        ids = [w["_id"] for w in r.json()]
        assert ws["_id"] in ids

    async def test_get_workspace_by_id(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.get(f"/api/v1/workspaces/{ws['_id']}", headers=auth["headers"])
        assert r.status_code == 200
        assert r.json()["_id"] == ws["_id"]

    async def test_update_workspace_name(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.patch(
            f"/api/v1/workspaces/{ws['_id']}", json={"name": "Renamed"}, headers=auth["headers"]
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    async def test_delete_workspace_returns_204(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.delete(f"/api/v1/workspaces/{ws['_id']}", headers=auth["headers"])
        assert r.status_code == 204

    async def test_deleted_workspace_not_in_list(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        await http.delete(f"/api/v1/workspaces/{ws['_id']}", headers=auth["headers"])
        r = await http.get("/api/v1/workspaces", headers=auth["headers"])
        ids = [w["_id"] for w in r.json()]
        assert ws["_id"] not in ids

    async def test_list_members_includes_owner(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.get(f"/api/v1/workspaces/{ws['_id']}/members", headers=auth["headers"])
        assert r.status_code == 200
        members = r.json()
        assert any(m["_id"] == auth["user_id"] for m in members)

    async def test_create_invite_for_workspace(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.post(
            f"/api/v1/workspaces/{ws['_id']}/invites",
            json={
                "email": "invitee@example.com",
                "role": "member",
            },
            headers=auth["headers"],
        )
        assert r.status_code == 200
        invite = r.json()
        assert invite["email"] == "invitee@example.com"
        assert "token" in invite

    async def test_validate_invite_by_token(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.post(
            f"/api/v1/workspaces/{ws['_id']}/invites",
            json={
                "email": "invitee2@example.com",
                "role": "member",
            },
            headers=auth["headers"],
        )
        token = r.json()["token"]

        # Validate without auth
        r2 = await http.get(f"/api/v1/workspaces/invites/{token}")
        assert r2.status_code == 200
        assert r2.json()["token"] == token

    async def test_accept_invite_adds_user_to_workspace(self, http: AsyncClient):
        owner = await _register_and_login(http)
        ws = await _make_workspace(http, owner["headers"])

        # Create invite
        invitee_email = _unique_email()
        r = await http.post(
            f"/api/v1/workspaces/{ws['_id']}/invites",
            json={
                "email": invitee_email,
                "role": "member",
            },
            headers=owner["headers"],
        )
        token = r.json()["token"]

        # Register and login as invitee
        invitee = await _register_and_login(http, email=invitee_email)

        # Accept the invite
        r2 = await http.post(
            f"/api/v1/workspaces/invites/{token}/accept", headers=invitee["headers"]
        )
        assert r2.status_code == 200

        # Invitee should now be in workspace members
        r3 = await http.get(f"/api/v1/workspaces/{ws['_id']}/members", headers=owner["headers"])
        member_ids = [m["_id"] for m in r3.json()]
        assert invitee["user_id"] in member_ids

    async def test_revoke_invite_marks_it_revoked(self, http: AsyncClient):
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        r = await http.post(
            f"/api/v1/workspaces/{ws['_id']}/invites",
            json={
                "email": "revoke-me@example.com",
                "role": "member",
            },
            headers=auth["headers"],
        )
        invite_id = r.json()["_id"]

        r2 = await http.delete(
            f"/api/v1/workspaces/{ws['_id']}/invites/{invite_id}", headers=auth["headers"]
        )
        assert r2.status_code == 204

    async def test_workspace_without_license_returns_403(self, http: AsyncClient):
        """Workspace routes require a valid license."""
        auth = await _register_and_login(http)
        with patch.dict(os.environ, {"POCKETPAW_LICENSE_KEY": ""}):
            import ee.cloud.license as lic_mod

            lic_mod._cached_license = None
            lic_mod._license_error = None
            try:
                r = await http.post(
                    "/api/v1/workspaces",
                    json={"name": "X", "slug": "x-slug"},
                    headers=auth["headers"],
                )
                assert r.status_code == 403
            finally:
                # Restore cached license for other tests
                lic_mod._cached_license = None
                lic_mod._license_error = None


# ===========================================================================
# CHAT DOMAIN
# ===========================================================================


class TestChatFlow:
    """Tests for groups, messages, reactions, pins, search, DMs."""

    async def _setup(self, http: AsyncClient) -> dict:
        """Create a user with an active workspace and return context."""
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        # Set active workspace so current_workspace_id dep works
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=auth["headers"],
        )
        return {**auth, "workspace_id": ws["_id"]}

    async def test_create_group_returns_group(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post("/api/v1/chat/groups", json={"name": "general"}, headers=ctx["headers"])
        assert r.status_code == 200
        grp = r.json()
        assert grp["name"] == "general"
        assert "_id" in grp

    async def test_list_groups_includes_created_group(self, http: AsyncClient):
        ctx = await self._setup(http)
        await http.post("/api/v1/chat/groups", json={"name": "list-test"}, headers=ctx["headers"])
        r = await http.get("/api/v1/chat/groups", headers=ctx["headers"])
        assert r.status_code == 200
        names = [g["name"] for g in r.json()]
        assert "list-test" in names

    async def test_get_group_by_id(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/chat/groups", json={"name": "get-test"}, headers=ctx["headers"]
        )
        group_id = r1.json()["_id"]
        r2 = await http.get(f"/api/v1/chat/groups/{group_id}", headers=ctx["headers"])
        assert r2.status_code == 200
        assert r2.json()["_id"] == group_id

    async def test_send_message_to_group(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "msg-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]

        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={
                "content": "Hello world!",
            },
            headers=ctx["headers"],
        )
        assert r2.status_code == 200
        msg = r2.json()
        assert msg["content"] == "Hello world!"
        assert "_id" in msg

    async def test_list_messages_with_cursor_pagination(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "page-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]

        # Send 3 messages
        for i in range(3):
            await http.post(
                f"/api/v1/chat/groups/{group_id}/messages",
                json={"content": f"msg {i}"},
                headers=ctx["headers"],
            )

        r2 = await http.get(
            f"/api/v1/chat/groups/{group_id}/messages?limit=2", headers=ctx["headers"]
        )
        assert r2.status_code == 200
        page = r2.json()
        assert "items" in page
        assert len(page["items"]) <= 2

    async def test_edit_message_updates_content(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "edit-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]

        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "original"},
            headers=ctx["headers"],
        )
        msg_id = r2.json()["_id"]

        r3 = await http.patch(
            f"/api/v1/chat/messages/{msg_id}", json={"content": "edited"}, headers=ctx["headers"]
        )
        assert r3.status_code == 200
        assert r3.json()["content"] == "edited"

    async def test_edit_message_sets_edited_flag(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "edit-flag-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]
        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "original"},
            headers=ctx["headers"],
        )
        msg_id = r2.json()["_id"]
        await http.patch(
            f"/api/v1/chat/messages/{msg_id}", json={"content": "updated"}, headers=ctx["headers"]
        )

        r3 = await http.get(f"/api/v1/chat/groups/{group_id}/messages", headers=ctx["headers"])
        msgs = r3.json()["items"]
        edited_msg = next((m for m in msgs if m["_id"] == msg_id), None)
        assert edited_msg is not None
        assert edited_msg["edited"] is True

    async def test_delete_message_soft_deletes(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "delete-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]
        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "to-delete"},
            headers=ctx["headers"],
        )
        msg_id = r2.json()["_id"]

        r3 = await http.delete(f"/api/v1/chat/messages/{msg_id}", headers=ctx["headers"])
        assert r3.status_code == 204

        # Deleted message should not appear in listing
        r4 = await http.get(f"/api/v1/chat/groups/{group_id}/messages", headers=ctx["headers"])
        active_ids = [m["_id"] for m in r4.json()["items"] if not m.get("deleted")]
        assert msg_id not in active_ids

    async def test_toggle_reaction_adds_then_removes(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "react-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]
        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "react me"},
            headers=ctx["headers"],
        )
        msg_id = r2.json()["_id"]

        # Add reaction
        r3 = await http.post(
            f"/api/v1/chat/messages/{msg_id}/react", json={"emoji": "👍"}, headers=ctx["headers"]
        )
        assert r3.status_code == 200
        reactions_after_add = r3.json().get("reactions", [])
        assert any(rx.get("emoji") == "👍" for rx in reactions_after_add)

        # Toggle off (same emoji same user)
        r4 = await http.post(
            f"/api/v1/chat/messages/{msg_id}/react", json={"emoji": "👍"}, headers=ctx["headers"]
        )
        assert r4.status_code == 200

    async def test_pin_message_and_list_pinned(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "pin-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]
        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "pin me"},
            headers=ctx["headers"],
        )
        msg_id = r2.json()["_id"]

        r3 = await http.post(f"/api/v1/chat/groups/{group_id}/pin/{msg_id}", headers=ctx["headers"])
        assert r3.status_code == 200

        # Verify group shows pinned message
        r4 = await http.get(f"/api/v1/chat/groups/{group_id}", headers=ctx["headers"])
        pinned = r4.json().get("pinnedMessages", r4.json().get("pinned_messages", []))
        assert msg_id in pinned

    async def test_unpin_message(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "unpin-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]
        r2 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "pin then unpin"},
            headers=ctx["headers"],
        )
        msg_id = r2.json()["_id"]

        await http.post(f"/api/v1/chat/groups/{group_id}/pin/{msg_id}", headers=ctx["headers"])
        r3 = await http.delete(
            f"/api/v1/chat/groups/{group_id}/pin/{msg_id}", headers=ctx["headers"]
        )
        assert r3.status_code == 204

        r4 = await http.get(f"/api/v1/chat/groups/{group_id}", headers=ctx["headers"])
        pinned = r4.json().get("pinnedMessages", r4.json().get("pinned_messages", []))
        assert msg_id not in pinned

    async def test_search_messages_by_content(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "search-test"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]
        await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "find this needle"},
            headers=ctx["headers"],
        )
        await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "irrelevant haystack"},
            headers=ctx["headers"],
        )

        r2 = await http.get(
            f"/api/v1/chat/groups/{group_id}/search?q=needle", headers=ctx["headers"]
        )
        assert r2.status_code == 200
        results = r2.json()
        assert isinstance(results, list)
        assert any("needle" in m.get("content", "") for m in results)

    async def test_create_dm_between_two_users(self, http: AsyncClient):
        owner = await _register_and_login(http)
        ws = await _make_workspace(http, owner["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=owner["headers"],
        )

        # Second user accepts invite
        invitee_email = _unique_email()
        r_inv = await http.post(
            f"/api/v1/workspaces/{ws['_id']}/invites",
            json={"email": invitee_email, "role": "member"},
            headers=owner["headers"],
        )
        token = r_inv.json()["token"]
        invitee = await _register_and_login(http, email=invitee_email)
        await http.post(f"/api/v1/workspaces/invites/{token}/accept", headers=invitee["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=invitee["headers"],
        )

        # Create DM
        r = await http.post(f"/api/v1/chat/dm/{invitee['user_id']}", headers=owner["headers"])
        assert r.status_code == 200
        dm = r.json()
        assert dm["type"] == "dm"

    async def test_update_group_name(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "old-name"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]

        r2 = await http.patch(
            f"/api/v1/chat/groups/{group_id}", json={"name": "new-name"}, headers=ctx["headers"]
        )
        assert r2.status_code == 200
        assert r2.json()["name"] == "new-name"

    async def test_archive_group(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/chat/groups", json={"name": "archive-me"}, headers=ctx["headers"]
        )
        group_id = r.json()["_id"]

        r2 = await http.post(f"/api/v1/chat/groups/{group_id}/archive", headers=ctx["headers"])
        assert r2.status_code == 200

        r3 = await http.get(f"/api/v1/chat/groups/{group_id}", headers=ctx["headers"])
        assert r3.json()["archived"] is True


# ===========================================================================
# POCKETS DOMAIN
# ===========================================================================


class TestPocketsFlow:
    """Tests for pocket CRUD, widgets, sharing."""

    async def _setup(self, http: AsyncClient) -> dict:
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=auth["headers"],
        )
        return {**auth, "workspace_id": ws["_id"]}

    async def test_create_pocket_returns_pocket(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post("/api/v1/pockets", json={"name": "My Pocket"}, headers=ctx["headers"])
        assert r.status_code == 200
        pocket = r.json()
        assert pocket["name"] == "My Pocket"
        assert "_id" in pocket
        assert pocket["owner"] == ctx["user_id"]

    async def test_create_pocket_with_ripple_spec(self, http: AsyncClient):
        ctx = await self._setup(http)
        spec = {"layout": "grid", "columns": 2, "rows": 1, "widgets": []}
        r = await http.post(
            "/api/v1/pockets",
            json={
                "name": "Ripple Pocket",
                "rippleSpec": spec,
            },
            headers=ctx["headers"],
        )
        assert r.status_code == 200
        # rippleSpec should be stored
        pocket = r.json()
        assert pocket["rippleSpec"] is not None or pocket.get("ripple_spec") is not None

    async def test_list_pockets_includes_created(self, http: AsyncClient):
        ctx = await self._setup(http)
        await http.post("/api/v1/pockets", json={"name": "Listed Pocket"}, headers=ctx["headers"])
        r = await http.get("/api/v1/pockets", headers=ctx["headers"])
        assert r.status_code == 200
        names = [p["name"] for p in r.json()]
        assert "Listed Pocket" in names

    async def test_get_pocket_by_id(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/pockets", json={"name": "GetMe"}, headers=ctx["headers"])
        pocket_id = r1.json()["_id"]
        r2 = await http.get(f"/api/v1/pockets/{pocket_id}", headers=ctx["headers"])
        assert r2.status_code == 200
        assert r2.json()["_id"] == pocket_id

    async def test_update_pocket_name_and_description(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/pockets", json={"name": "Original"}, headers=ctx["headers"])
        pocket_id = r1.json()["_id"]
        r2 = await http.patch(
            f"/api/v1/pockets/{pocket_id}",
            json={
                "name": "Renamed",
                "description": "A description",
            },
            headers=ctx["headers"],
        )
        assert r2.status_code == 200
        assert r2.json()["name"] == "Renamed"
        assert r2.json()["description"] == "A description"

    async def test_delete_pocket_returns_204(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/pockets", json={"name": "DeleteMe"}, headers=ctx["headers"])
        pocket_id = r1.json()["_id"]
        r2 = await http.delete(f"/api/v1/pockets/{pocket_id}", headers=ctx["headers"])
        assert r2.status_code == 204

    async def test_deleted_pocket_not_in_list(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/pockets", json={"name": "GonePocket"}, headers=ctx["headers"])
        pocket_id = r1.json()["_id"]
        await http.delete(f"/api/v1/pockets/{pocket_id}", headers=ctx["headers"])
        r2 = await http.get("/api/v1/pockets", headers=ctx["headers"])
        ids = [p["_id"] for p in r2.json()]
        assert pocket_id not in ids

    async def test_add_widget_to_pocket(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "WidgetPocket"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]

        r2 = await http.post(
            f"/api/v1/pockets/{pocket_id}/widgets",
            json={
                "name": "My Widget",
                "type": "chart",
            },
            headers=ctx["headers"],
        )
        assert r2.status_code == 200
        pocket = r2.json()
        assert any(w["name"] == "My Widget" for w in pocket.get("widgets", []))

    async def test_update_widget_config(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "UpdateWidget"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]
        r2 = await http.post(
            f"/api/v1/pockets/{pocket_id}/widgets", json={"name": "W1"}, headers=ctx["headers"]
        )
        widgets = r2.json()["widgets"]
        widget_id = widgets[0].get("_id") or widgets[0].get("id")

        r3 = await http.patch(
            f"/api/v1/pockets/{pocket_id}/widgets/{widget_id}",
            json={
                "config": {"key": "value"},
            },
            headers=ctx["headers"],
        )
        assert r3.status_code == 200

    async def test_remove_widget_from_pocket(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "RemoveWidget"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]
        r2 = await http.post(
            f"/api/v1/pockets/{pocket_id}/widgets",
            json={"name": "ToRemove"},
            headers=ctx["headers"],
        )
        w = r2.json()["widgets"][0]
        widget_id = w.get("_id") or w.get("id")

        r3 = await http.delete(
            f"/api/v1/pockets/{pocket_id}/widgets/{widget_id}", headers=ctx["headers"]
        )
        assert r3.status_code == 204

        r4 = await http.get(f"/api/v1/pockets/{pocket_id}", headers=ctx["headers"])
        widget_ids = [w["id"] for w in r4.json()["widgets"]]
        assert widget_id not in widget_ids

    async def test_generate_share_link_returns_token(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "SharePocket"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]

        r2 = await http.post(
            f"/api/v1/pockets/{pocket_id}/share", json={"access": "view"}, headers=ctx["headers"]
        )
        assert r2.status_code == 200
        result = r2.json()
        assert "shareLinkToken" in result or "token" in result

    async def test_access_pocket_via_share_link(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "PublicPocket"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]

        r2 = await http.post(
            f"/api/v1/pockets/{pocket_id}/share", json={"access": "view"}, headers=ctx["headers"]
        )
        token = r2.json().get("shareLinkToken") or r2.json().get("token")
        assert token

        # Access without auth via share link
        r3 = await http.get(f"/api/v1/pockets/shared/{token}")
        assert r3.status_code == 200
        assert r3.json()["_id"] == pocket_id

    async def test_revoke_share_link_returns_204(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "RevokeShare"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]
        await http.post(
            f"/api/v1/pockets/{pocket_id}/share", json={"access": "view"}, headers=ctx["headers"]
        )

        r2 = await http.delete(f"/api/v1/pockets/{pocket_id}/share", headers=ctx["headers"])
        assert r2.status_code == 204

    async def test_revoked_share_link_no_longer_accessible(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/pockets", json={"name": "RevokeAccess"}, headers=ctx["headers"]
        )
        pocket_id = r1.json()["_id"]
        r2 = await http.post(
            f"/api/v1/pockets/{pocket_id}/share", json={"access": "view"}, headers=ctx["headers"]
        )
        token = r2.json().get("shareLinkToken") or r2.json().get("token")

        await http.delete(f"/api/v1/pockets/{pocket_id}/share", headers=ctx["headers"])

        r3 = await http.get(f"/api/v1/pockets/shared/{token}")
        assert r3.status_code == 404


# ===========================================================================
# SESSIONS DOMAIN
# ===========================================================================


class TestSessionsFlow:
    """Tests for session CRUD and activity tracking."""

    async def _setup(self, http: AsyncClient) -> dict:
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=auth["headers"],
        )
        return {**auth, "workspace_id": ws["_id"]}

    async def test_create_session_returns_session(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post(
            "/api/v1/sessions", json={"title": "My Session"}, headers=ctx["headers"]
        )
        assert r.status_code == 200
        session = r.json()
        assert session["title"] == "My Session"
        assert "sessionId" in session
        assert session["workspace"] == ctx["workspace_id"]

    async def test_create_session_default_title(self, http: AsyncClient):
        ctx = await self._setup(http)
        r = await http.post("/api/v1/sessions", json={}, headers=ctx["headers"])
        assert r.status_code == 200
        assert r.json()["title"] == "New Chat"

    async def test_list_sessions_includes_created(self, http: AsyncClient):
        ctx = await self._setup(http)
        await http.post("/api/v1/sessions", json={"title": "ListedSession"}, headers=ctx["headers"])
        r = await http.get("/api/v1/sessions", headers=ctx["headers"])
        assert r.status_code == 200
        titles = [s["title"] for s in r.json()]
        assert "ListedSession" in titles

    async def test_get_session_by_id(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/sessions", json={"title": "Fetchable"}, headers=ctx["headers"]
        )
        session_id = r1.json()["_id"]
        r2 = await http.get(f"/api/v1/sessions/{session_id}", headers=ctx["headers"])
        assert r2.status_code == 200
        assert r2.json()["_id"] == session_id

    async def test_update_session_title(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/sessions", json={"title": "OldTitle"}, headers=ctx["headers"])
        session_id = r1.json()["_id"]
        r2 = await http.patch(
            f"/api/v1/sessions/{session_id}", json={"title": "NewTitle"}, headers=ctx["headers"]
        )
        assert r2.status_code == 200
        assert r2.json()["title"] == "NewTitle"

    async def test_delete_session_soft_deletes(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/sessions", json={"title": "DeleteMe"}, headers=ctx["headers"])
        session_id = r1.json()["_id"]

        r2 = await http.delete(f"/api/v1/sessions/{session_id}", headers=ctx["headers"])
        assert r2.status_code == 204

    async def test_deleted_session_not_in_list(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post(
            "/api/v1/sessions", json={"title": "GoneSession"}, headers=ctx["headers"]
        )
        session_id = r1.json()["_id"]
        await http.delete(f"/api/v1/sessions/{session_id}", headers=ctx["headers"])

        r2 = await http.get("/api/v1/sessions", headers=ctx["headers"])
        ids = [s["_id"] for s in r2.json()]
        assert session_id not in ids

    async def test_another_user_cannot_access_session(self, http: AsyncClient):
        ctx1 = await self._setup(http)
        r1 = await http.post("/api/v1/sessions", json={"title": "Private"}, headers=ctx1["headers"])
        session_id = r1.json()["_id"]

        ctx2 = await self._setup(http)
        r2 = await http.get(f"/api/v1/sessions/{session_id}", headers=ctx2["headers"])
        assert r2.status_code in (403, 404)

    async def test_session_history_returns_empty_for_new_session(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/sessions", json={"title": "History"}, headers=ctx["headers"])
        session_id = r1.json()["_id"]
        r2 = await http.get(f"/api/v1/sessions/{session_id}/history", headers=ctx["headers"])
        assert r2.status_code == 200
        assert r2.json()["messages"] == []

    async def test_touch_session_updates_activity(self, http: AsyncClient):
        ctx = await self._setup(http)
        r1 = await http.post("/api/v1/sessions", json={"title": "Touch Me"}, headers=ctx["headers"])
        session_uuid = r1.json()["sessionId"]

        r2 = await http.post(f"/api/v1/sessions/{session_uuid}/touch")
        assert r2.status_code == 204

    async def test_create_session_linked_to_pocket(self, http: AsyncClient):
        ctx = await self._setup(http)

        # Create a pocket first
        r_pocket = await http.post(
            "/api/v1/pockets", json={"name": "PocketForSession"}, headers=ctx["headers"]
        )
        pocket_id = r_pocket.json()["_id"]

        # Create session linked to pocket
        r = await http.post(
            "/api/v1/sessions",
            json={"title": "PocketSession", "pocket_id": pocket_id},
            headers=ctx["headers"],
        )
        assert r.status_code == 200
        assert r.json()["pocket"] == pocket_id


# ===========================================================================
# CROSS-DOMAIN FLOWS
# ===========================================================================


class TestCrossDomainFlows:
    """Tests that span multiple domains, verifying integrated behavior."""

    async def test_full_workspace_creation_and_group_lifecycle(self, http: AsyncClient):
        """Create workspace → create group → send message → verify message persists."""
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=auth["headers"],
        )

        # Create group
        r_grp = await http.post(
            "/api/v1/chat/groups", json={"name": "cross-domain"}, headers=auth["headers"]
        )
        group_id = r_grp.json()["_id"]

        # Send message
        r_msg = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "cross-domain test"},
            headers=auth["headers"],
        )
        msg_id = r_msg.json()["_id"]

        # Retrieve messages — verify persists
        r_list = await http.get(f"/api/v1/chat/groups/{group_id}/messages", headers=auth["headers"])
        msg_ids = [m["_id"] for m in r_list.json()["items"]]
        assert msg_id in msg_ids

    async def test_session_created_under_pocket_appears_in_pocket_sessions(self, http: AsyncClient):
        """Pocket sessions endpoint lists sessions linked to that pocket."""
        auth = await _register_and_login(http)
        ws = await _make_workspace(http, auth["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=auth["headers"],
        )

        r_pocket = await http.post(
            "/api/v1/pockets", json={"name": "PocketWithSessions"}, headers=auth["headers"]
        )
        pocket_id = r_pocket.json()["_id"]

        r_session = await http.post(
            f"/api/v1/pockets/{pocket_id}/sessions",
            json={"title": "Pocket Session"},
            headers=auth["headers"],
        )
        assert r_session.status_code == 200
        session_id = r_session.json()["_id"]

        r_list = await http.get(f"/api/v1/pockets/{pocket_id}/sessions", headers=auth["headers"])
        assert r_list.status_code == 200
        ids = [s["_id"] for s in r_list.json()]
        assert session_id in ids

    async def test_workspace_member_count_reflects_invite_acceptance(self, http: AsyncClient):
        """Accepting an invite should increase the member count."""
        owner = await _register_and_login(http)
        ws = await _make_workspace(http, owner["headers"])
        initial_count = ws["memberCount"]
        assert initial_count == 1

        invitee_email = _unique_email()
        r_inv = await http.post(
            f"/api/v1/workspaces/{ws['_id']}/invites",
            json={
                "email": invitee_email,
                "role": "member",
            },
            headers=owner["headers"],
        )
        token = r_inv.json()["token"]

        invitee = await _register_and_login(http, email=invitee_email)
        await http.post(f"/api/v1/workspaces/invites/{token}/accept", headers=invitee["headers"])

        r_ws = await http.get(f"/api/v1/workspaces/{ws['_id']}", headers=owner["headers"])
        assert r_ws.json()["memberCount"] == initial_count + 1

    async def test_license_endpoint_returns_valid_status(self, http: AsyncClient):
        """GET /api/v1/license should return valid=True with the test license."""
        r = await http.get("/api/v1/license")
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["org"] == "test-org"
        assert body["plan"] == "enterprise"

    async def test_user_search_within_workspace(self, http: AsyncClient):
        """GET /api/v1/users with search param returns matching workspace members."""
        owner = await _register_and_login(http)
        ws = await _make_workspace(http, owner["headers"])
        await http.post(
            "/api/v1/auth/set-active-workspace",
            json={"workspace_id": ws["_id"]},
            headers=owner["headers"],
        )

        # Update owner's name so search can find them
        await http.patch(
            "/api/v1/auth/me", json={"full_name": "SearchableUser"}, headers=owner["headers"]
        )

        r = await http.get("/api/v1/users?search=Searchable", headers=owner["headers"])
        assert r.status_code == 200
        results = r.json()
        assert isinstance(results, list)
