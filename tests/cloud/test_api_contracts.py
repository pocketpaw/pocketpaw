"""API contract tests — pin response shapes before the unified-schema rewrite.

Purpose:
    These tests snapshot the exact JSON shape of the ee/cloud endpoints that
    the schema rewrite must preserve. They run against the current
    (pre-rewrite) code and MUST stay green after T2. Any field rename, new
    field, or removed field breaks these tests by design.

Coverage:
    - POST /api/v1/chat/groups/{id}/messages          (MessageResponse shape)
    - GET  /api/v1/chat/groups/{id}/messages          (CursorPage shape)
    - GET  /api/v1/sessions                           (SessionResponse list)
    - GET  /api/v1/sessions/runtime                   (runtime sessions envelope)
    - GET  /api/v1/sessions/{id}/history              (history envelope)

Infrastructure:
    Uses the same real-MongoDB-on-localhost pattern as tests/cloud/test_e2e_api.py.
    Fixtures are duplicated locally to keep this file self-contained.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Expected shapes — frozen. Any change here MUST be intentional and reviewed.
# ---------------------------------------------------------------------------

MESSAGE_RESPONSE_KEYS = frozenset(
    {
        "_id",
        "group",
        "sender",
        "senderType",
        "agent",
        "content",
        "mentions",
        "replyTo",
        "replyPreview",
        "threadCount",
        "attachments",
        "reactions",
        "edited",
        "editedAt",
        "deleted",
        "createdAt",
    }
)

CURSOR_PAGE_KEYS = frozenset({"items", "nextCursor", "hasMore"})

SESSION_RESPONSE_KEYS = frozenset(
    {
        "_id",
        "sessionId",
        "workspace",
        "owner",
        "title",
        "pocket",
        "group",
        "agent",
        "surface",  # added: which UI surface minted this session
        "messageCount",
        "lastActivity",
        "createdAt",
        "deletedAt",
    }
)

RUNTIME_SESSIONS_ENVELOPE_KEYS = frozenset({"sessions", "total"})

HISTORY_ENVELOPE_KEYS = frozenset({"messages"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_license_key(secret: str = "test-secret") -> str:
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


def _assert_iso8601(value: object, field: str) -> None:
    assert isinstance(value, str), f"{field} must be str, got {type(value).__name__}"
    # fromisoformat handles "+00:00" and "Z"-less ISO; accept both.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed is not None, f"{field} must be ISO-8601"


def _unique_email() -> str:
    return f"contract-{uuid.uuid4().hex[:8]}@test.example"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def license_env():
    secret = "test-secret"
    key = _make_license_key(secret)
    env = {
        "POCKETPAW_LICENSE_KEY": key,
        "POCKETPAW_LICENSE_SECRET": secret,
        "AUTH_SECRET": "test-auth-secret-for-contracts",
    }
    with patch.dict(os.environ, env):
        yield env


@pytest.fixture()
async def beanie_db():
    import pocketpaw_ee.cloud.license as lic_mod
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    lic_mod._cached_license = None
    lic_mod._license_error = None

    db_name = f"test_contracts_{uuid.uuid4().hex[:8]}"
    conn_str = f"mongodb://localhost:27017/{db_name}"
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    await init_beanie(connection_string=conn_str, document_models=ALL_DOCUMENTS)
    yield client[db_name]
    await client.drop_database(db_name)


@pytest.fixture()
async def app(license_env, beanie_db) -> FastAPI:
    import pocketpaw_ee.cloud.license as lic_mod
    from pocketpaw_ee.cloud import mount_cloud

    lic_mod._cached_license = None

    test_app = FastAPI()

    mock_pool = MagicMock()
    mock_pool.start = AsyncMock()
    mock_pool.stop = AsyncMock()

    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=mock_pool):
        mount_cloud(test_app)

    yield test_app


@pytest.fixture()
async def http(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.fixture()
async def auth_ctx(http: AsyncClient) -> dict:
    """Register a fresh user, create a workspace, set it active. Return ctx."""
    email = _unique_email()
    password = "Password1!"
    r = await http.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Contract Tester"},
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    r = await http.post(
        "/api/v1/auth/bearer/login",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    slug = f"ws-{uuid.uuid4().hex[:8]}"
    r = await http.post(
        "/api/v1/workspaces",
        json={"name": "Contract WS", "slug": slug},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    ws = r.json()

    await http.post(
        "/api/v1/auth/set-active-workspace",
        json={"workspace_id": ws["_id"]},
        headers=headers,
    )

    return {
        "user_id": user_id,
        "headers": headers,
        "workspace_id": ws["_id"],
    }


# ===========================================================================
# MESSAGE CONTRACTS — POST + GET
# ===========================================================================


class TestMessageContract:
    """Pin the shape of `/api/v1/chat/groups/{id}/messages` request/response."""

    async def _create_group(self, http: AsyncClient, ctx: dict) -> str:
        r = await http.post(
            "/api/v1/chat/groups",
            json={"name": f"contract-{uuid.uuid4().hex[:6]}"},
            headers=ctx["headers"],
        )
        assert r.status_code == 200, r.text
        return r.json()["_id"]

    async def test_send_message_response_shape(self, http: AsyncClient, auth_ctx: dict):
        group_id = await self._create_group(http, auth_ctx)

        r = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "contract-pin"},
            headers=auth_ctx["headers"],
        )
        assert r.status_code == 200, r.text
        msg = r.json()

        # Exhaustive key match — no extras, no missing.
        assert set(msg.keys()) == MESSAGE_RESPONSE_KEYS, (
            f"expected {MESSAGE_RESPONSE_KEYS}, got {set(msg.keys())}"
        )

        # Types + value invariants
        assert isinstance(msg["_id"], str) and len(msg["_id"]) == 24
        assert msg["group"] == group_id
        assert msg["sender"] == auth_ctx["user_id"]
        assert msg["senderType"] == "user"
        assert msg["agent"] is None
        assert msg["content"] == "contract-pin"
        assert msg["mentions"] == []
        assert msg["replyTo"] is None
        assert msg["attachments"] == []
        assert msg["reactions"] == []
        assert msg["edited"] is False
        assert msg["editedAt"] is None
        assert msg["deleted"] is False
        _assert_iso8601(msg["createdAt"], "createdAt")

    async def test_send_message_with_mentions_and_reply_shape(
        self, http: AsyncClient, auth_ctx: dict
    ):
        group_id = await self._create_group(http, auth_ctx)

        # Seed a parent message to reply to
        r0 = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={"content": "parent"},
            headers=auth_ctx["headers"],
        )
        parent_id = r0.json()["_id"]

        r = await http.post(
            f"/api/v1/chat/groups/{group_id}/messages",
            json={
                "content": "reply body",
                "reply_to": parent_id,
                "mentions": [{"type": "user", "id": auth_ctx["user_id"], "display_name": "@me"}],
            },
            headers=auth_ctx["headers"],
        )
        assert r.status_code == 200, r.text
        msg = r.json()

        assert set(msg.keys()) == MESSAGE_RESPONSE_KEYS
        assert msg["replyTo"] == parent_id
        assert isinstance(msg["mentions"], list)
        assert len(msg["mentions"]) == 1
        mention = msg["mentions"][0]
        # Mention shape comes from the Mention sub-model — also pin it.
        assert set(mention.keys()) == {"type", "id", "display_name"}
        assert mention["type"] == "user"
        assert mention["id"] == auth_ctx["user_id"]
        assert mention["display_name"] == "@me"

    async def test_list_messages_cursor_page_shape(self, http: AsyncClient, auth_ctx: dict):
        group_id = await self._create_group(http, auth_ctx)
        for i in range(3):
            await http.post(
                f"/api/v1/chat/groups/{group_id}/messages",
                json={"content": f"m{i}"},
                headers=auth_ctx["headers"],
            )

        r = await http.get(
            f"/api/v1/chat/groups/{group_id}/messages?limit=2",
            headers=auth_ctx["headers"],
        )
        assert r.status_code == 200, r.text
        page = r.json()

        # Envelope shape
        assert set(page.keys()) == CURSOR_PAGE_KEYS
        assert isinstance(page["items"], list)
        assert isinstance(page["hasMore"], bool)
        # nextCursor is str when hasMore, None otherwise — exercise both by asking for 2 of 3
        assert page["hasMore"] is True
        assert isinstance(page["nextCursor"], str)
        assert "|" in page["nextCursor"]  # format "{iso}|{oid}"

        # Item shape — each item conforms to MESSAGE_RESPONSE_KEYS
        assert len(page["items"]) == 2
        for item in page["items"]:
            assert set(item.keys()) == MESSAGE_RESPONSE_KEYS
            _assert_iso8601(item["createdAt"], "items[].createdAt")

        # DESC ordering — newer first
        ts0 = datetime.fromisoformat(page["items"][0]["createdAt"].replace("Z", "+00:00"))
        ts1 = datetime.fromisoformat(page["items"][1]["createdAt"].replace("Z", "+00:00"))
        assert ts0 >= ts1

    async def test_list_messages_empty_page_shape(self, http: AsyncClient, auth_ctx: dict):
        group_id = await self._create_group(http, auth_ctx)
        r = await http.get(
            f"/api/v1/chat/groups/{group_id}/messages",
            headers=auth_ctx["headers"],
        )
        assert r.status_code == 200, r.text
        page = r.json()
        assert set(page.keys()) == CURSOR_PAGE_KEYS
        assert page["items"] == []
        assert page["hasMore"] is False
        assert page["nextCursor"] is None


# ===========================================================================
# SESSION CONTRACTS — list, runtime, history
# ===========================================================================


class TestSessionContract:
    """Pin the shape of `/api/v1/sessions*` responses."""

    async def test_create_and_list_session_shape(self, http: AsyncClient, auth_ctx: dict):
        # Create a session
        r = await http.post(
            "/api/v1/sessions",
            json={"title": "Contract Session"},
            headers=auth_ctx["headers"],
        )
        assert r.status_code == 200, r.text
        created = r.json()

        # Single-session create response shape
        assert set(created.keys()) == SESSION_RESPONSE_KEYS, (
            f"expected {SESSION_RESPONSE_KEYS}, got {set(created.keys())}"
        )
        assert isinstance(created["_id"], str) and len(created["_id"]) == 24
        assert isinstance(created["sessionId"], str)
        assert created["workspace"] == auth_ctx["workspace_id"]
        assert created["owner"] == auth_ctx["user_id"]
        assert created["title"] == "Contract Session"
        assert created["pocket"] is None
        assert created["group"] is None
        assert created["agent"] is None
        assert created["messageCount"] == 0
        _assert_iso8601(created["lastActivity"], "lastActivity")
        _assert_iso8601(created["createdAt"], "createdAt")
        assert created["deletedAt"] is None

        # List response shape
        r2 = await http.get("/api/v1/sessions", headers=auth_ctx["headers"])
        assert r2.status_code == 200, r2.text
        items = r2.json()
        assert isinstance(items, list)
        assert any(s["_id"] == created["_id"] for s in items)
        for item in items:
            assert set(item.keys()) == SESSION_RESPONSE_KEYS

    async def test_runtime_sessions_envelope_shape(self, http: AsyncClient, auth_ctx: dict):
        r = await http.get("/api/v1/sessions/runtime", headers=auth_ctx["headers"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == RUNTIME_SESSIONS_ENVELOPE_KEYS
        assert isinstance(body["sessions"], list)
        assert isinstance(body["total"], int)
        assert body["total"] >= 0

    async def test_session_history_empty_envelope_shape(self, http: AsyncClient, auth_ctx: dict):
        # Create a session then fetch its history (expected empty — no messages sent)
        r = await http.post(
            "/api/v1/sessions",
            json={"title": "History Session"},
            headers=auth_ctx["headers"],
        )
        session_pk = r.json()["sessionId"]

        r2 = await http.get(
            f"/api/v1/sessions/{session_pk}/history",
            headers=auth_ctx["headers"],
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert set(body.keys()) == HISTORY_ENVELOPE_KEYS
        assert isinstance(body["messages"], list)
        # No writes → empty. Post-rewrite we expect the same.
        assert body["messages"] == []

    # NOTE: pre-rewrite, GET /api/v1/sessions/{id}/history ONLY reads file
    # memory; it never hits the service-layer Mongo hydration path. So the
    # history-item shape (_id/role/content/sender/senderType/createdAt) is
    # unreachable via this endpoint today. Post-rewrite the endpoint WILL
    # return items from Mongo — at that point, add a follow-up test pinning
    # the item shape. For now we pin only the envelope (see empty test above).
