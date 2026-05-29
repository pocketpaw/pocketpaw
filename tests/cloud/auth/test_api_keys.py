"""Tests for workspace API keys + bearer resolver (Wave 3 Task 8)."""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.context import RequestContext, request_context, require_scope
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import api_keys as api_keys_service
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.models.api_key import APIKey
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

_EMAIL_OWNER = "owner@example.com"
_EMAIL_OUTSIDER = "outsider@example.com"
_PASSWORD = "StrongPass123!"
_WORKSPACE_ID = "ws_test_1"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")

    probe = APIRouter()

    @probe.get("/probe/ctx")
    async def probe_ctx(ctx: RequestContext = Depends(request_context)) -> dict:
        return {"user_id": ctx.user_id, "workspace_id": ctx.workspace_id, "scopes": ctx.scopes}

    @probe.get("/probe/chat-send")
    async def probe_chat_send(
        ctx: RequestContext = Depends(require_scope("chat.send")),
    ) -> dict:
        return {"ok": True, "user_id": ctx.user_id, "scopes": ctx.scopes}

    app.include_router(probe, prefix="/api/v1")
    return app


async def _seed_user(email: str, *, member: bool = True) -> User:
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=email, password=_PASSWORD))
        break
    if member:
        user.workspaces = [
            WorkspaceMembership(workspace=_WORKSPACE_ID, role="owner", joined_at=datetime.now(UTC))
        ]
        user.active_workspace = _WORKSPACE_ID
        await user.save()
    return user


@pytest_asyncio.fixture
async def env(mongo_db, monkeypatch):  # noqa: ARG001
    owner = await _seed_user(_EMAIL_OWNER, member=True)
    outsider = await _seed_user(_EMAIL_OUTSIDER, member=False)
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    api_keys_service._reset_caches_for_tests()
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield {"client": client, "owner": owner, "outsider": outsider, "app": app}


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code in (200, 204), resp.text
    return resp.cookies["paw_auth"]


@pytest.mark.asyncio
async def test_create_api_key_returns_full_key_and_persists_hash(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    resp = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "ci", "scopes": ["chat.read"], "expires_in_days": 7},
        cookies={"paw_auth": cookie},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fullKey"].startswith("paw_")
    assert body["prefix"] == body["fullKey"][4:12]
    assert body["scopes"] == ["chat.read"]
    assert body["revoked"] is False

    rows = await APIKey.find_all().to_list()
    assert len(rows) == 1
    doc = rows[0]
    assert doc.hashed_secret != body["fullKey"]
    assert doc.hashed_secret != body["fullKey"][4:]
    assert doc.hashed_secret.startswith("$argon2")
    assert doc.workspace == _WORKSPACE_ID
    assert doc.expires_at is not None


@pytest.mark.asyncio
async def test_non_member_cannot_create_key(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OUTSIDER)
    resp = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "x", "scopes": ["chat.read"], "expires_in_days": None},
        cookies={"paw_auth": cookie},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_scope_rejected(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    resp = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "x", "scopes": ["nope.scope"], "expires_in_days": None},
        cookies={"paw_auth": cookie},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_excludes_revoked(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)

    a = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "a", "scopes": ["chat.read"]},
        cookies={"paw_auth": cookie},
    )
    b = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "b", "scopes": ["chat.read"]},
        cookies={"paw_auth": cookie},
    )
    assert a.status_code == 200 and b.status_code == 200

    revoke = await client.delete(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys/{b.json()['id']}",
        cookies={"paw_auth": cookie},
    )
    assert revoke.status_code == 200

    listed = await client.get(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        cookies={"paw_auth": cookie},
    )
    assert listed.status_code == 200
    ids = [k["id"] for k in listed.json()]
    assert a.json()["id"] in ids
    assert b.json()["id"] not in ids


@pytest.mark.asyncio
async def test_bearer_with_paw_key_resolves(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    created = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "k", "scopes": ["chat.read", "files.read"]},
        cookies={"paw_auth": cookie},
    )
    full = created.json()["fullKey"]

    resp = await client.get(
        "/api/v1/probe/ctx",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["workspace_id"] == _WORKSPACE_ID
    assert payload["scopes"] == ["chat.read", "files.read"]


@pytest.mark.asyncio
async def test_bad_paw_token_returns_401(env) -> None:
    client = env["client"]
    resp = await client.get(
        "/api/v1/probe/ctx",
        headers={"Authorization": "Bearer paw_deadbeefdeadbeefdeadbeefdeadbeef"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_rejects_subsequent_use(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    created = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "k", "scopes": ["chat.read"]},
        cookies={"paw_auth": cookie},
    )
    full = created.json()["fullKey"]
    key_id = created.json()["id"]

    ok = await client.get(
        "/api/v1/probe/ctx",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert ok.status_code == 200

    revoke = await client.delete(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys/{key_id}",
        cookies={"paw_auth": cookie},
    )
    assert revoke.status_code == 200

    blocked = await client.get(
        "/api/v1/probe/ctx",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert blocked.status_code == 401


@pytest.mark.asyncio
async def test_expired_key_rejected(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    created = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "k", "scopes": ["chat.read"]},
        cookies={"paw_auth": cookie},
    )
    full = created.json()["fullKey"]
    key_id = created.json()["id"]

    doc = await APIKey.get(key_id)
    assert doc is not None
    doc.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await doc.save()

    resp = await client.get(
        "/api/v1/probe/ctx",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_require_scope_blocks_missing_and_allows_jwt(env) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    # JWT auth — scopes is None → pass.
    via_jwt = await client.get("/api/v1/probe/chat-send", cookies={"paw_auth": cookie})
    assert via_jwt.status_code == 200

    # API key without chat.send → 403.
    created = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "no-send", "scopes": ["chat.read"]},
        cookies={"paw_auth": cookie},
    )
    full = created.json()["fullKey"]
    blocked = await client.get(
        "/api/v1/probe/chat-send",
        headers={"Authorization": f"Bearer {full}"},
    )
    assert blocked.status_code == 403

    # API key with chat.send → pass.
    created2 = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "with-send", "scopes": ["chat.read", "chat.send"]},
        cookies={"paw_auth": cookie},
    )
    full2 = created2.json()["fullKey"]
    allowed = await client.get(
        "/api/v1/probe/chat-send",
        headers={"Authorization": f"Bearer {full2}"},
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_last_used_at_rate_limited(env, monkeypatch) -> None:
    client = env["client"]
    cookie = await _login(client, _EMAIL_OWNER)
    created = await client.post(
        f"/api/v1/workspaces/{_WORKSPACE_ID}/api-keys",
        json={"name": "k", "scopes": ["chat.read"]},
        cookies={"paw_auth": cookie},
    )
    full = created.json()["fullKey"]
    key_id = created.json()["id"]

    # Drive the monotonic clock so consecutive calls within 60s do not
    # rewrite last_used_at. Each request reads monotonic at most once
    # (inside resolve_bearer); the audit sampler uses its own module-level
    # time import so it does not consume from this iterator.
    clock = {"now": 1000.0}

    class _FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock["now"]

    monkeypatch.setattr(api_keys_service, "time", _FakeTime)

    r1 = await client.get("/api/v1/probe/ctx", headers={"Authorization": f"Bearer {full}"})
    assert r1.status_code == 200
    doc1 = await APIKey.get(key_id)
    assert doc1 is not None and doc1.last_used_at is not None
    t1 = doc1.last_used_at

    clock["now"] = 1010.0  # < 60s later
    r2 = await client.get("/api/v1/probe/ctx", headers={"Authorization": f"Bearer {full}"})
    assert r2.status_code == 200
    doc2 = await APIKey.get(key_id)
    assert doc2 is not None and doc2.last_used_at == t1  # unchanged

    clock["now"] = 2000.0  # > 60s later
    r3 = await client.get("/api/v1/probe/ctx", headers={"Authorization": f"Bearer {full}"})
    assert r3.status_code == 200
    doc3 = await APIKey.get(key_id)
    assert doc3 is not None and doc3.last_used_at != t1  # updated after >60s
