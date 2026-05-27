"""Tests for per-session tracking + revoke endpoints (Wave 3 Task 6)."""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.models.auth_session import AuthSession

_EMAIL = "alice@example.com"
_PASSWORD = "StrongPass123!"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_user() -> None:
    async for db in get_user_db():
        manager = UserManager(db)
        await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))
        break


@pytest_asyncio.fixture
async def env(mongo_db, monkeypatch):  # noqa: ARG001
    await _seed_user()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _login(client: AsyncClient, *, ua: str | None = None) -> AsyncClient:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if ua is not None:
        headers["User-Agent"] = ua
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": _EMAIL, "password": _PASSWORD},
        headers=headers,
    )
    assert resp.status_code in (200, 204), resp.text
    assert "paw_auth" in resp.cookies
    return resp


def _make_client_with_cookie(app, cookie: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://t", cookies={"paw_auth": cookie})


@pytest.mark.asyncio
async def test_login_records_one_session_per_login(env) -> None:
    client = env
    await _login(client, ua="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0")
    # New client = fresh cookie jar so the second login mints a separate token.
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c2:
        await _login(c2, ua="Mozilla/5.0 (Macintosh) Firefox/118.0")

    rows = await AuthSession.find_all().to_list()
    assert len(rows) == 2
    labels = {r.device_label for r in rows}
    assert "Chrome · Windows" in labels
    assert "Firefox · macOS" in labels


@pytest.mark.asyncio
async def test_list_sessions_marks_current(env) -> None:
    client = env
    r1 = await _login(client, ua="Mozilla/5.0 (Windows) Chrome/120")
    current_cookie = r1.cookies["paw_auth"]

    # Second login with a different cookie jar.
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c2:
        await _login(c2, ua="Mozilla/5.0 (Macintosh) Safari/17.0")

    resp = await client.get("/api/v1/auth/sessions", cookies={"paw_auth": current_cookie})
    assert resp.status_code == 200, resp.text
    sessions = resp.json()
    assert len(sessions) == 2
    current = [s for s in sessions if s["is_current"]]
    assert len(current) == 1
    assert current[0]["device_label"] == "Chrome · Windows"


@pytest.mark.asyncio
async def test_revoke_session_blocks_subsequent_auth(env) -> None:
    client = env
    r1 = await _login(client, ua="Mozilla/5.0 (Windows) Chrome/120")
    current_cookie = r1.cookies["paw_auth"]

    app2 = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://t") as c2:
        r2 = await _login(c2, ua="Mozilla/5.0 (Macintosh) Safari/17.0")
        other_cookie = r2.cookies["paw_auth"]

    # Pull jti list from /sessions; revoke the non-current one.
    resp = await client.get("/api/v1/auth/sessions", cookies={"paw_auth": current_cookie})
    other = next(s for s in resp.json() if not s["is_current"])

    rdel = await client.delete(
        f"/api/v1/auth/sessions/{other['jti']}", cookies={"paw_auth": current_cookie}
    )
    assert rdel.status_code == 200, rdel.text

    # The revoked cookie should now fail auth.
    me = await client.get("/api/v1/auth/me", cookies={"paw_auth": other_cookie})
    assert me.status_code == 401

    # Current cookie still works.
    me_ok = await client.get("/api/v1/auth/me", cookies={"paw_auth": current_cookie})
    assert me_ok.status_code == 200


@pytest.mark.asyncio
async def test_revoke_others_keeps_current(env) -> None:
    client = env
    r1 = await _login(client, ua="UA-1 Chrome/120 Windows")
    current_cookie = r1.cookies["paw_auth"]

    app2 = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://t") as c2:
        r2 = await _login(c2, ua="UA-2 Safari/17 Macintosh")
        other_cookie = r2.cookies["paw_auth"]

    app3 = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app3), base_url="http://t") as c3:
        await _login(c3, ua="UA-3 Firefox/118 Linux")

    resp = await client.post(
        "/api/v1/auth/sessions/revoke-others", cookies={"paw_auth": current_cookie}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked"] == 2

    # Current still authorised.
    me = await client.get("/api/v1/auth/me", cookies={"paw_auth": current_cookie})
    assert me.status_code == 200

    # Other rejected.
    other = await client.get("/api/v1/auth/me", cookies={"paw_auth": other_cookie})
    assert other.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_current_session(env) -> None:
    client = env
    r1 = await _login(client, ua="UA Chrome Windows")
    cookie = r1.cookies["paw_auth"]

    out = await client.post("/api/v1/auth/logout", cookies={"paw_auth": cookie})
    assert out.status_code in (200, 204)

    me = await client.get("/api/v1/auth/me", cookies={"paw_auth": cookie})
    assert me.status_code == 401

    # AuthSession row flagged revoked.
    rows = await AuthSession.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].revoked is True
    assert rows[0].revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_unknown_jti_returns_404(env) -> None:
    client = env
    r1 = await _login(client, ua="UA Chrome Windows")
    cookie = r1.cookies["paw_auth"]

    resp = await client.delete("/api/v1/auth/sessions/does-not-exist", cookies={"paw_auth": cookie})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_is_revoked_falls_back_to_mongo_on_redis_failure(env, monkeypatch) -> None:
    """Revocation must still hold when Redis is unavailable.

    Earlier behaviour returned False on Redis failure (fail-open), which
    silently un-revoked every kicked/logged-out session for the duration
    of the outage. The fallback now reads ``AuthSession.revoked`` from
    Mongo so durable revocation state takes over.
    """
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.cloud.auth import sessions as _sessions

    client = env
    r1 = await _login(client)
    cookie = r1.cookies["paw_auth"]

    # Revoke the current session normally (touches both Redis + Mongo).
    sessions_list = await client.get("/api/v1/auth/sessions", cookies={"paw_auth": cookie})
    target_jti = sessions_list.json()[0]["jti"]
    user_id = (await AuthSession.find_one(AuthSession.jti == target_jti)).user_id

    # Confirm revocation visible via the normal Redis path.
    await client.delete(f"/api/v1/auth/sessions/{target_jti}", cookies={"paw_auth": cookie})
    assert await _sessions.is_revoked(user_id, target_jti) is True

    # Now simulate a Redis outage. Mongo still says revoked=True, so the
    # backstop should return True (not False as the old fail-open did).
    class _Broken:
        async def exists(self, *_a, **_k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", lambda: _Broken())
    assert await _sessions.is_revoked(user_id, target_jti) is True
