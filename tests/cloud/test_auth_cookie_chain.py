# tests/cloud/test_auth_cookie_chain.py
# Created: 2026-05-17 (security #1117 P1) — Asserts that the JWT auth
#   chain accepts BOTH the ``paw_auth`` HttpOnly cookie and the legacy
#   ``Authorization: Bearer`` header during the cookie-mode rollout.
#   Bearer is intentionally kept live for back-compat with the Tauri
#   client and MCP/script callers; see ee/cloud/auth/router.py for the
#   deprecation note.
#
#   These tests exercise the real fastapi-users login + /auth/me chain
#   against a mongomock-motor user DB so we hit the actual cookie /
#   bearer transports rather than the test-only dependency overrides.

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud._core.csrf import AUTH_COOKIE_NAME, CSRFMiddleware
from ee.cloud._core.http import add_error_handler
from ee.cloud.auth.core import (
    UserCreate,
    UserManager,
    bearer_backend,
    cookie_backend,
    cookie_transport,
    fastapi_users,
    get_user_db,
)
from ee.cloud.auth.router import router as auth_router

_TEST_EMAIL = "cookie-chain@example.com"
_TEST_PASSWORD = "test-password-123"


async def _seed_user() -> None:
    """Create a real fastapi-users-managed user so the login path can
    verify the hashed password and mint a JWT."""

    async for db in get_user_db():
        manager = UserManager(db)
        await manager.create(
            UserCreate(
                email=_TEST_EMAIL,
                password=_TEST_PASSWORD,
                is_verified=True,
            ),
        )
        return


def _build_app() -> FastAPI:
    """Spin up an app with the real auth router + CSRF middleware
    (so the cookie path actually runs through both layers)."""

    app = FastAPI()
    add_error_handler(app)
    # CSRF middleware mirrors the production stack — auth endpoints are
    # exempt by path, so login/logout/me work without CSRF tokens.
    app.add_middleware(CSRFMiddleware)
    app.include_router(auth_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def app_client(mongo_db) -> AsyncClient:
    await _seed_user()
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Cookie attributes on login
# ---------------------------------------------------------------------------


def test_cookie_transport_pins_httponly_and_name() -> None:
    """Spec-level assertion: the cookie name + httponly flag must not
    drift. Drift here means tokens become readable by JS, which is the
    exact vector P1 is closing."""

    assert cookie_transport.cookie_name == AUTH_COOKIE_NAME == "paw_auth"
    assert cookie_transport.cookie_httponly is True
    assert cookie_transport.cookie_samesite == "lax"


async def test_login_sets_httponly_cookie(app_client: AsyncClient) -> None:
    """The cookie backend's login response must set the paw_auth cookie
    with HttpOnly + SameSite=Lax. Secure is env-driven so we don't
    assert on it here — production deploys flip POCKETPAW_AUTH_COOKIE_SECURE=true."""

    resp = await app_client.post(
        "/api/v1/auth/login",
        data={"username": _TEST_EMAIL, "password": _TEST_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code in (200, 204), resp.text

    set_cookie = resp.headers.get("set-cookie", "")
    assert AUTH_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    # SameSite case-insensitive in different httpx versions.
    assert "samesite=lax" in set_cookie.lower()


# ---------------------------------------------------------------------------
# Cookie-only authentication path
# ---------------------------------------------------------------------------


async def test_cookie_only_authenticates(app_client: AsyncClient) -> None:
    """After login, the cookie alone (no Authorization header) must
    authenticate subsequent requests. Today this works because the
    cookie backend is registered first in the FastAPIUsers stack; this
    test pins that invariant so a future reordering can't silently break
    the web build."""

    login = await app_client.post(
        "/api/v1/auth/login",
        data={"username": _TEST_EMAIL, "password": _TEST_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert login.status_code in (200, 204), login.text
    # httpx auto-stores Set-Cookie into the client jar.
    assert AUTH_COOKIE_NAME in app_client.cookies

    me = await app_client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == _TEST_EMAIL


# ---------------------------------------------------------------------------
# Bearer back-compat path
# ---------------------------------------------------------------------------


async def test_bearer_only_still_works(app_client: AsyncClient) -> None:
    """The /auth/bearer/login endpoint returns a JSON body with the JWT;
    callers should still be able to use it as an Authorization header.
    Bearer is the Tauri/MCP/script path — P1 keeps it live, P2 migrates
    Tauri to the OS keychain, and only after that audit do we drop it."""

    login = await app_client.post(
        "/api/v1/auth/bearer/login",
        data={"username": _TEST_EMAIL, "password": _TEST_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert login.status_code == 200, login.text
    token = login.json().get("access_token")
    assert token, "bearer login should return access_token in JSON body"

    # Drop the cookie jar so we know the bearer header is what authenticates.
    app_client.cookies.clear()

    me = await app_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == _TEST_EMAIL


# ---------------------------------------------------------------------------
# Logout clears the cookie
# ---------------------------------------------------------------------------


async def test_logout_clears_cookie(app_client: AsyncClient) -> None:
    await app_client.post(
        "/api/v1/auth/login",
        data={"username": _TEST_EMAIL, "password": _TEST_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert AUTH_COOKIE_NAME in app_client.cookies

    logout = await app_client.post("/api/v1/auth/logout")
    assert logout.status_code in (200, 204), logout.text

    # The cookie's Set-Cookie header on logout has Max-Age=0 (or the
    # equivalent expires-in-the-past attribute). Either way httpx drops
    # it from the jar.
    set_cookie = logout.headers.get("set-cookie", "")
    assert AUTH_COOKIE_NAME in set_cookie
    assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()


# ---------------------------------------------------------------------------
# Backend list ordering — pin against drift
# ---------------------------------------------------------------------------


def test_fastapi_users_has_both_backends_registered() -> None:
    """The FastAPIUsers instance must carry the cookie backend (so the
    web build can authenticate via Set-Cookie) AND the bearer backend
    (so the Tauri client and MCP callers keep working). Dropping
    either silently breaks one client class."""

    backend_names = {b.name for b in fastapi_users.authenticator.backends}
    assert {cookie_backend.name, bearer_backend.name} <= backend_names
