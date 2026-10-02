"""The login/register brute-force guard keys on (client IP, submitted email).

``_auth_dispatch`` puts POSTs to /api/v1/auth/{login,register,bearer/login}
through ``login_limiter`` (5 per 15 min) keyed ``login:{ip}:{email}``. Login
posts an OAuth2 form (field ``username``); register posts JSON (field
``email``). Starlette's ``request.form()`` does not raise on a JSON body, it
returns an empty FormData, so the guard must pick its parser from the
Content-Type. If it reads JSON as a form, every signup from one IP collapses
into the ``login:{ip}:`` bucket and the sixth distinct signup is 429'd.

The stub routes echo the body, which also proves the cached body is replayed
to the downstream handler.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from pocketpaw.dashboard_auth import AuthMiddleware
from pocketpaw.security.rate_limiter import RateLimiter

_LIMITED = "Too many login attempts"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.post("/api/v1/auth/register")
    async def _register(request: Request) -> dict:
        return {"body": (await request.body()).decode()}

    @app.post("/api/v1/auth/login")
    async def _login(request: Request) -> dict:
        form = await request.form()
        return {"username": form.get("username")}

    return app


@pytest.fixture
async def client():
    # rate=0: no refill, so "6th attempt in a bucket -> 429" is deterministic.
    # Fresh limiters per test; auth_limiter/api_limiter are made roomy so only
    # the login_limiter under test can produce a 429.
    transport = ASGITransport(app=_build_app(), client=("203.0.113.9", 40000))
    with (
        patch("pocketpaw.dashboard_auth.login_limiter", RateLimiter(rate=0.0, capacity=5)),
        patch("pocketpaw.dashboard_auth.auth_limiter", RateLimiter(rate=0.0, capacity=1000)),
        patch("pocketpaw.dashboard_auth.api_limiter", RateLimiter(rate=0.0, capacity=1000)),
        patch("pocketpaw.dashboard_auth.get_access_token", return_value="oss-master-token"),
    ):
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


def _limited(res) -> bool:
    return res.status_code == 429 and res.json().get("detail") == _LIMITED


@pytest.mark.asyncio
async def test_json_registers_with_distinct_emails_do_not_share_a_bucket(client) -> None:
    for i in range(8):
        res = await client.post(
            "/api/v1/auth/register",
            json={"email": f"user{i}@example.com", "password": "pw-123456"},
        )
        assert not _limited(res), f"signup {i} with a fresh email was login-limited"
        assert res.status_code == 200
        assert f"user{i}@example.com" in res.json()["body"]  # body replayed downstream


@pytest.mark.asyncio
async def test_json_registers_with_the_same_email_are_limited(client) -> None:
    results = [
        await client.post(
            "/api/v1/auth/register",
            json={"email": "Same@Example.com", "password": "pw-123456"},
        )
        for _ in range(6)
    ]
    assert not any(_limited(r) for r in results[:5])
    assert _limited(results[5])
    # A different email from the same IP is its own bucket.
    other = await client.post("/api/v1/auth/register", json={"email": "other@example.com"})
    assert not _limited(other)


@pytest.mark.asyncio
async def test_form_login_with_the_same_username_is_limited(client) -> None:
    results = [
        await client.post(
            "/api/v1/auth/login",
            data={"username": "victim@example.com", "password": "guess"},
        )
        for _ in range(6)
    ]
    for r in results[:5]:
        assert not _limited(r)
        assert r.json() == {"username": "victim@example.com"}  # form replayed
    assert _limited(results[5])
    other = await client.post(
        "/api/v1/auth/login", data={"username": "someone@example.com", "password": "x"}
    )
    assert not _limited(other)
