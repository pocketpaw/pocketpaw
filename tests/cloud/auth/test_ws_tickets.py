"""Tests for the WebSocket ticket flow (mint endpoint + consume helper).

Tickets exist so the cross-subdomain SPA can authenticate the /ws/cloud
upgrade without putting the long-lived JWT in the URL: client mints a
30-second single-use ticket via its cookie session, passes it as
``?token=``, server consumes it atomically from Redis on accept.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import fakeredis.aioredis
import jwt as pyjwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import SECRET, UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.auth.ws_tickets import consume_ws_ticket, mint_ws_ticket

_EMAIL = "ticket-user@example.com"
_PASSWORD = "StrongPass123!"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_user() -> str:
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))
        return str(user.id)
    raise RuntimeError("user db iterator exhausted")  # pragma: no cover


@pytest_asyncio.fixture
async def env(mongo_db, monkeypatch):  # noqa: ARG001
    user_id = await _seed_user()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, user_id


async def _login(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": _EMAIL, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code in (200, 204), resp.text
    assert "paw_auth" in resp.cookies


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_endpoint_requires_auth(env) -> None:
    client, _ = env
    resp = await client.post("/api/v1/auth/ws/ticket")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ticket_endpoint_returns_ticket_for_authed_user(env) -> None:
    client, user_id = env
    await _login(client)
    resp = await client.post("/api/v1/auth/ws/ticket")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("ticket"), str) and body["ticket"]

    # Ticket decodes to the same user with the ws audience.
    payload = pyjwt.decode(body["ticket"], SECRET, algorithms=["HS256"], audience=["ws"])
    assert payload["sub"] == user_id
    assert payload["type"] == "ws_ticket"
    assert payload["exp"] - payload["iat"] <= 30


# ---------------------------------------------------------------------------
# Consume helper (the half called from the WS handler)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_succeeds_once(env) -> None:
    _, user_id = env
    ticket = await mint_ws_ticket(user_id)
    assert await consume_ws_ticket(ticket) == user_id


@pytest.mark.asyncio
async def test_consume_rejects_replay(env) -> None:
    _, user_id = env
    ticket = await mint_ws_ticket(user_id)
    assert await consume_ws_ticket(ticket) == user_id
    # Second consume must fail — single-use guarantee.
    assert await consume_ws_ticket(ticket) is None


@pytest.mark.asyncio
async def test_consume_rejects_wrong_audience(env) -> None:
    _, user_id = env
    # Mint a token shaped like a session JWT, not a ws ticket.
    bogus = pyjwt.encode(
        {
            "sub": user_id,
            "aud": ["fastapi-users:auth"],
            "jti": "x",
            "exp": int(time.time()) + 30,
        },
        SECRET,
        algorithm="HS256",
    )
    assert await consume_ws_ticket(bogus) is None


@pytest.mark.asyncio
async def test_consume_rejects_expired(env, monkeypatch) -> None:
    _, user_id = env
    # Forge an expired ticket directly (don't wait 30s in CI).
    expired = pyjwt.encode(
        {
            "sub": user_id,
            "type": "ws_ticket",
            "aud": ["ws"],
            "jti": "expired-jti",
            "iat": int(time.time()) - 60,
            "exp": int(time.time()) - 30,
        },
        SECRET,
        algorithm="HS256",
    )
    # Even if the jti happens to be in Redis, decode fails first.
    assert await consume_ws_ticket(expired) is None


@pytest.mark.asyncio
async def test_consume_rejects_unminted(env) -> None:
    _, user_id = env
    # Valid signature + audience, but no Redis registration → consume fails.
    fake = pyjwt.encode(
        {
            "sub": user_id,
            "type": "ws_ticket",
            "aud": ["ws"],
            "jti": "never-minted",
            "iat": int(time.time()),
            "exp": int(time.time()) + 30,
        },
        SECRET,
        algorithm="HS256",
    )
    assert await consume_ws_ticket(fake) is None


@pytest.mark.asyncio
async def test_consume_rejects_tampered_signature(env) -> None:
    _, user_id = env
    ticket = await mint_ws_ticket(user_id)
    tampered = ticket[:-4] + ("AAAA" if ticket[-4:] != "AAAA" else "BBBB")
    assert await consume_ws_ticket(tampered) is None
