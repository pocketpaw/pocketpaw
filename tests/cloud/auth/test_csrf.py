"""CSRF wiring for Wave 3 Task 13.

Focused on the login + logout cookie-lifecycle pieces added in Task 13.
The middleware-level reject/exempt behaviour is covered in
``tests/cloud/test_csrf_middleware.py`` which predates this task.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pyotp
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.csrf import (
    AUTH_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    CSRFMiddleware,
    mint_csrf_token,
)
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.models.user import User

_EMAIL = "csrf-task13@example.com"
_EMAIL_MFA = "csrf-task13-mfa@example.com"
_PASSWORD = "StrongPass123!"
_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_users() -> None:
    async for db in get_user_db():
        manager = UserManager(db)
        await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))
        await manager.create(UserCreate(email=_EMAIL_MFA, password=_PASSWORD))
        break

    mfa_user = await User.find_one(User.email == _EMAIL_MFA)
    assert mfa_user is not None
    mfa_user.mfa_totp_secret = _TOTP_SECRET
    mfa_user.mfa_enabled = True
    mfa_user.mfa_backup_codes = []
    await mfa_user.save()


@pytest_asyncio.fixture
async def env(mongo_db):  # noqa: ARG001 — forces Beanie init
    await _seed_users()
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _login(client: AsyncClient, email: str = _EMAIL) -> object:
    return await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------------------
# mint_csrf_token helper
# ---------------------------------------------------------------------------


def test_mint_csrf_token_returns_unique_urlsafe_string() -> None:
    a = mint_csrf_token()
    b = mint_csrf_token()
    assert a and b and a != b
    assert "+" not in a and "/" not in a  # URL-safe base64


# ---------------------------------------------------------------------------
# Login mints paw_csrf
# ---------------------------------------------------------------------------


async def test_cookie_login_sets_paw_csrf(env) -> None:
    client = env
    res = await _login(client)
    assert res.status_code in (200, 204), res.text
    # Both cookies arrive together — that's the whole point of minting on login.
    assert AUTH_COOKIE_NAME in res.cookies
    assert CSRF_COOKIE_NAME in res.cookies
    assert res.cookies[CSRF_COOKIE_NAME]

    raw = res.headers.get_list("set-cookie")
    csrf_header = next(c for c in raw if c.startswith(f"{CSRF_COOKIE_NAME}="))
    assert "HttpOnly" not in csrf_header  # JS must be able to read it
    assert "SameSite=lax" in csrf_header.lower() or "samesite=lax" in csrf_header.lower()


async def test_bearer_login_does_not_set_paw_csrf(env) -> None:
    """Bearer transport never sets paw_auth — there's no cookie to pair with."""
    client = env
    res = await client.post(
        "/api/v1/auth/bearer/login",
        data={"username": _EMAIL, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 200, res.text
    assert CSRF_COOKIE_NAME not in res.cookies


async def test_mfa_challenge_sets_paw_csrf(env) -> None:
    client = env
    res = await _login(client, _EMAIL_MFA)
    assert res.status_code == 200
    mfa_token = res.json()["mfa_token"]
    code = pyotp.TOTP(_TOTP_SECRET).now()
    res2 = await client.post(
        "/api/v1/auth/mfa/challenge",
        json={"mfa_token": mfa_token, "code": code},
    )
    assert res2.status_code in (200, 204), res2.text
    assert AUTH_COOKIE_NAME in res2.cookies
    assert CSRF_COOKIE_NAME in res2.cookies


# ---------------------------------------------------------------------------
# Logout clears paw_csrf
# ---------------------------------------------------------------------------


async def test_cookie_logout_clears_paw_csrf(env) -> None:
    client = env
    await _login(client)
    assert client.cookies.get(CSRF_COOKIE_NAME)
    csrf = client.cookies[CSRF_COOKIE_NAME]

    res = await client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code in (200, 204)
    # The Set-Cookie header should expire paw_csrf explicitly.
    raw = res.headers.get_list("set-cookie")
    csrf_set = [c for c in raw if c.startswith(f"{CSRF_COOKIE_NAME}=")]
    assert csrf_set, raw
    expired = csrf_set[0].lower()
    assert "max-age=0" in expired or "expires=thu, 01 jan 1970" in expired


async def test_bearer_logout_clears_paw_csrf(env) -> None:
    """Even on the bearer path we clear paw_csrf — a user that swapped from
    cookie to bearer mid-session shouldn't leave the dangling cookie behind."""
    client = env
    # First get a bearer token.
    res = await client.post(
        "/api/v1/auth/bearer/login",
        data={"username": _EMAIL, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = res.json()["access_token"]

    res2 = await client.post(
        "/api/v1/auth/bearer/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res2.status_code in (200, 204)
    raw = res2.headers.get_list("set-cookie")
    csrf_set = [c for c in raw if c.startswith(f"{CSRF_COOKIE_NAME}=")]
    assert csrf_set, raw
