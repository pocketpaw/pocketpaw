"""One authenticated request resolves its user once, and never serves a stale one.

The EE auth bridge verifies the JWT (signature, audience, exp, revocation) and
loads the active user, then stashes ``(token, user)`` in a per-request scope.
``RevocableJWTStrategy.read_token`` serves that user only on an exact token
match under the same key/audience/algorithm; anything else takes the full
decode + revocation + ``User.get`` path. What must hold:

  * a GET that mixes ``current_optional_user`` and ``current_active_user`` costs
    one decode, one revocation check and one ``User.get`` in total;
  * the stash lives for one request only: nothing leaks into the next request
    or into a task spawned from the request that outlives it;
  * a revoked, inactive or deleted user is never stashed, and a token revoked
    between two requests is rejected on the second;
  * a different token, or a strategy with a different key, never reads the stash;
  * the three pure-ASGI layers pass streaming responses and websockets through
    byte-for-byte.

Mutations in ``tests/mutations/auth_request_path.json`` break each of these.
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_SECRET", "test-bridge-secret-do-not-use-in-prod")
os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import ee_auth_bridge, redis_client
from pocketpaw_ee.cloud._core.csrf import CSRFMiddleware
from pocketpaw_ee.cloud._core.ee_auth_bridge import EEAuthBridgeMiddleware, stashed_user
from pocketpaw_ee.cloud._core.request_log import RequestLogMiddleware
from pocketpaw_ee.cloud.auth import sessions as sessions_service
from pocketpaw_ee.cloud.auth.core import (
    SECRET,
    RevocableJWTStrategy,
    current_active_user,
    current_optional_user,
    get_jwt_strategy,
)
from pocketpaw_ee.cloud.models.user import User

from pocketpaw.dashboard_auth import AuthMiddleware
from pocketpaw.security.rate_limiter import RateLimiter

_spawned: list[asyncio.Task] = []


def _build_app() -> FastAPI:
    app = FastAPI()
    # As mount_cloud stacks them: the OSS AuthMiddleware (a BaseHTTPMiddleware,
    # so the route runs in a child task with a COPIED context) inside the bridge.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(EEAuthBridgeMiddleware)

    @app.get("/api/v1/both")
    async def _both(
        a: User | None = Depends(current_optional_user),
        b: User = Depends(current_active_user),
    ) -> dict[str, Any]:
        return {"a": str(a.id) if a else None, "b": str(b.id), "same": a is b}

    @app.get("/api/v1/optional")
    async def _optional(a: User | None = Depends(current_optional_user)) -> dict[str, Any]:
        return {"a": str(a.id) if a else None}

    @app.get("/api/v1/stash")
    async def _stash(token: str = "") -> dict[str, Any]:
        user = stashed_user(token, get_jwt_strategy())
        return {"stashed": str(user.id) if user is not None else None}

    @app.get("/api/v1/spawn")
    async def _spawn(token: str = "") -> dict[str, Any]:
        # A task spawned by the request copies its context and can outlive it.
        async def _later() -> Any:
            await asyncio.sleep(0.05)
            return stashed_user(token, get_jwt_strategy())

        _spawned.append(asyncio.ensure_future(_later()))
        return {"ok": True}

    return app


async def _seed(email: str, *, is_active: bool = True) -> User:
    user = User(email=email, hashed_password="x", is_active=is_active, is_verified=True)
    await user.insert()
    return user


async def _jwt(user: User) -> str:
    return await RevocableJWTStrategy(secret=SECRET, lifetime_seconds=60).write_token(user)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    return fake


@pytest_asyncio.fixture
async def client(mongo_db, fake_redis):  # noqa: ARG001 — Beanie + fakeredis
    limiter = RateLimiter(rate=1e6, capacity=10**6)
    transport = ASGITransport(app=_build_app(), client=("203.0.113.7", 40000))
    with (
        patch("pocketpaw.dashboard_auth.api_limiter", limiter),
        patch("pocketpaw.dashboard_auth.get_access_token", return_value="oss-master-token"),
    ):
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The cost: one decode, one revocation check, one User.get
# ---------------------------------------------------------------------------


async def test_mixed_user_dependencies_resolve_the_user_once(client) -> None:
    user = await _seed("once@t.test")
    token = await _jwt(user)
    real_get = User.get
    with (
        patch("jwt.decode", wraps=jwt.decode) as decode,
        patch.object(
            sessions_service, "is_revoked", AsyncMock(wraps=sessions_service.is_revoked)
        ) as revoked,
        patch.object(User, "get", AsyncMock(wraps=real_get)) as get,
    ):
        r = await client.get("/api/v1/both", headers=_bearer(token))

    assert r.status_code == 200, r.text
    assert r.json() == {"a": str(user.id), "b": str(user.id), "same": True}
    assert decode.call_count == 1
    assert revoked.await_count == 1
    assert get.await_count == 1


async def test_the_cookie_transport_is_served_from_the_stash_too(client) -> None:
    user = await _seed("cookie@t.test")
    token = await _jwt(user)
    real_get = User.get
    with patch.object(User, "get", AsyncMock(wraps=real_get)) as get:
        client.cookies.set("paw_auth", token)
        r = await client.get("/api/v1/both")
        client.cookies.clear()
    assert r.status_code == 200, r.text
    assert get.await_count == 1


# ---------------------------------------------------------------------------
# What is stashed, and for how long
# ---------------------------------------------------------------------------


async def test_the_stash_holds_the_user_for_the_request_that_verified_it(client) -> None:
    user = await _seed("stash@t.test")
    token = await _jwt(user)
    r = await client.get("/api/v1/stash", params={"token": token}, headers=_bearer(token))
    assert r.json() == {"stashed": str(user.id)}


async def test_the_stash_is_cleared_per_request(client) -> None:
    user = await _seed("perreq@t.test")
    token = await _jwt(user)
    await client.get("/api/v1/stash", params={"token": token}, headers=_bearer(token))

    # ASGITransport awaits the app in THIS task, so a scope that was never
    # reset would still be visible here and in the next request.
    assert ee_auth_bridge._request_scope.get() is None
    r = await client.get("/api/v1/stash", params={"token": token})
    assert r.json() == {"stashed": None}


async def test_a_task_that_outlives_its_request_cannot_read_the_stash(client) -> None:
    user = await _seed("spawn@t.test")
    token = await _jwt(user)
    _spawned.clear()
    r = await client.get("/api/v1/spawn", params={"token": token}, headers=_bearer(token))
    assert r.status_code == 200
    assert await _spawned[0] is None


async def test_a_token_revoked_between_two_requests_is_rejected_on_the_second(client) -> None:
    user = await _seed("revoke@t.test")
    token = await _jwt(user)
    assert (await client.get("/api/v1/both", headers=_bearer(token))).status_code == 200

    jti = jwt.decode(token, options={"verify_signature": False})["jti"]
    await sessions_service._mark_revoked(str(user.id), jti)

    assert (await client.get("/api/v1/both", headers=_bearer(token))).status_code == 401
    r = await client.get("/api/v1/stash", params={"token": token}, headers=_bearer(token))
    assert r.json() == {"stashed": None}


async def test_an_inactive_user_is_never_stashed(client) -> None:
    user = await _seed("inactive@t.test", is_active=False)
    token = await _jwt(user)
    r = await client.get("/api/v1/stash", params={"token": token}, headers=_bearer(token))
    assert r.json() == {"stashed": None}
    assert (await client.get("/api/v1/both", headers=_bearer(token))).status_code == 401


async def test_a_deleted_user_is_never_stashed(client) -> None:
    user = await _seed("deleted@t.test")
    token = await _jwt(user)
    await user.delete()
    r = await client.get("/api/v1/stash", params={"token": token}, headers=_bearer(token))
    assert r.json() == {"stashed": None}
    assert (await client.get("/api/v1/both", headers=_bearer(token))).status_code == 401


async def test_a_different_token_never_reads_the_stash(client) -> None:
    alice = await _seed("alice@t.test")
    bob = await _seed("bob@t.test")
    alice_token, bob_token = await _jwt(alice), await _jwt(bob)
    # Alice authenticates the request; the route asks about Bob's token.
    r = await client.get("/api/v1/stash", params={"token": bob_token}, headers=_bearer(alice_token))
    assert r.json() == {"stashed": None}


async def test_a_strategy_with_another_key_never_reads_the_stash(client) -> None:
    user = await _seed("otherkey@t.test")
    token = await _jwt(user)
    other = RevocableJWTStrategy(secret="a-different-secret-entirely", lifetime_seconds=60)
    scope = ee_auth_bridge._RequestScope(memo=None)
    scope.token, scope.user = token, user
    scope.verifier = ee_auth_bridge._verifier_of(get_jwt_strategy())
    reset = ee_auth_bridge._request_scope.set(scope)
    try:
        assert stashed_user(token, get_jwt_strategy()) is user
        assert stashed_user(token, other) is None
        # And read_token under the other key falls through to a real decode,
        # which rejects a token signed with the original secret.
        assert await other.read_token(token, AsyncMock()) is None
    finally:
        ee_auth_bridge._request_scope.reset(reset)


async def test_read_token_returns_the_user_type_fastapi_users_expects(client) -> None:
    user = await _seed("type@t.test")
    token = await _jwt(user)
    r = await client.get("/api/v1/optional", headers=_bearer(token))
    assert r.json() == {"a": str(user.id)}
    scope = ee_auth_bridge._RequestScope(memo=None)
    scope.token, scope.user = token, user
    scope.verifier = ee_auth_bridge._verifier_of(get_jwt_strategy())
    reset = ee_auth_bridge._request_scope.set(scope)
    try:
        served = await get_jwt_strategy().read_token(token, AsyncMock())
    finally:
        ee_auth_bridge._request_scope.reset(reset)
    assert served is user and isinstance(served, User)


# ---------------------------------------------------------------------------
# Pure ASGI: streaming and websockets pass through untouched
# ---------------------------------------------------------------------------


def _streaming_app(*, wrapped: bool) -> FastAPI:
    app = FastAPI()
    if wrapped:
        app.add_middleware(EEAuthBridgeMiddleware)
        app.add_middleware(RequestLogMiddleware)
        app.add_middleware(CSRFMiddleware)

    @app.get("/api/v1/stream")
    async def _stream() -> StreamingResponse:
        async def _chunks():
            for part in (b"alpha\n", b"beta\n", b"gamma\n"):
                yield part

        return StreamingResponse(_chunks(), media_type="text/plain", headers={"x-k": "v"})

    @app.websocket("/ws/echo")
    async def _echo(ws: WebSocket) -> None:
        await ws.accept()
        msg = await ws.receive_text()
        await ws.send_json(
            {
                "echo": msg,
                "scope_open": ee_auth_bridge._request_scope.get() is not None,
                "state": sorted(ws.scope.get("state", {}) or {}),
            }
        )
        await ws.close()

    return app


def test_streaming_responses_pass_through_byte_for_byte() -> None:
    bare = TestClient(_streaming_app(wrapped=False)).get("/api/v1/stream")
    with patch("pocketpaw_ee.cloud._core.request_log._log_request"):
        wrapped = TestClient(_streaming_app(wrapped=True)).get(
            "/api/v1/stream", headers={"Authorization": "Bearer not-a-jwt"}
        )
    assert wrapped.status_code == bare.status_code == 200
    assert wrapped.content == bare.content == b"alpha\nbeta\ngamma\n"
    assert wrapped.headers.raw == bare.headers.raw


def test_websockets_pass_through_the_stack_untouched() -> None:
    client = TestClient(_streaming_app(wrapped=True))
    client.cookies.set("paw_auth", "not-a-jwt")
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("hi")
        got = ws.receive_json()
    assert got == {"echo": "hi", "scope_open": False, "state": []}
