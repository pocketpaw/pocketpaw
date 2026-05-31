"""Tests for MFA-gated login flow (Wave 3 Task 4).

/auth/login (cookie) and /auth/bearer/login both check ``mfa_enabled``.
If set, the response is ``{mfa_required: True, mfa_token: ...}`` with no
session cookie / bearer token. The client then exchanges that token via
/auth/mfa/challenge for the real session.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import jwt as pyjwt
import pyotp
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import mfa as mfa_service
from pocketpaw_ee.cloud.auth.core import SECRET, UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.models.user import User

_EMAIL_PLAIN = "plain@example.com"
_EMAIL_MFA = "mfa@example.com"
_PASSWORD = "StrongPass123!"
_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_users() -> tuple[User, list[str]]:
    """Create one MFA-less user and one MFA-enabled user. Returns the MFA user
    along with the plaintext backup codes so tests can exercise them."""
    async for db in get_user_db():
        manager = UserManager(db)
        await manager.create(UserCreate(email=_EMAIL_PLAIN, password=_PASSWORD))
        await manager.create(UserCreate(email=_EMAIL_MFA, password=_PASSWORD))
        break

    mfa_user = await User.find_one(User.email == _EMAIL_MFA)
    assert mfa_user is not None
    plaintext, hashed = mfa_service.generate_backup_codes(n=3)
    mfa_user.mfa_totp_secret = _TOTP_SECRET
    mfa_user.mfa_enabled = True
    mfa_user.mfa_backup_codes = hashed
    await mfa_user.save()
    return mfa_user, plaintext


@pytest_asyncio.fixture
async def env(mongo_db):  # noqa: ARG001 — forces Beanie init
    mfa_user, backup_codes = await _seed_users()
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, mfa_user, backup_codes


async def _login(client: AsyncClient, email: str, path: str = "/api/v1/auth/login"):
    return await client.post(
        path,
        data={"username": email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------------------
# Login (cookie)
# ---------------------------------------------------------------------------


async def test_login_without_mfa_sets_cookie(env) -> None:
    client, _, _ = env
    resp = await _login(client, _EMAIL_PLAIN)
    assert resp.status_code in (200, 204), resp.text
    assert "paw_auth" in resp.cookies
    # No MFA gate body.
    if resp.content:
        assert "mfa_required" not in resp.text


async def test_login_with_mfa_returns_pending_token(env) -> None:
    client, _, _ = env
    resp = await _login(client, _EMAIL_MFA)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mfa_required"] is True
    assert isinstance(body["mfa_token"], str) and body["mfa_token"]
    assert "paw_auth" not in resp.cookies


async def test_login_bearer_with_mfa_returns_pending_token(env) -> None:
    client, _, _ = env
    resp = await _login(client, _EMAIL_MFA, path="/api/v1/auth/bearer/login")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mfa_required"] is True
    assert "mfa_token" in body
    # The real bearer response shape includes access_token; ensure it's NOT there.
    assert "access_token" not in body


async def test_login_bad_credentials(env) -> None:
    client, _, _ = env
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": _EMAIL_PLAIN, "password": "wrong-WRONG-9999!"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "LOGIN_BAD_CREDENTIALS"


# ---------------------------------------------------------------------------
# /auth/mfa/challenge
# ---------------------------------------------------------------------------


async def _get_mfa_token(client: AsyncClient) -> str:
    resp = await _login(client, _EMAIL_MFA)
    return resp.json()["mfa_token"]


async def test_challenge_with_valid_totp_sets_cookie(env) -> None:
    client, _, _ = env
    token = await _get_mfa_token(client)
    code = pyotp.TOTP(_TOTP_SECRET).now()
    resp = await client.post("/api/v1/auth/mfa/challenge", json={"mfa_token": token, "code": code})
    assert resp.status_code in (200, 204), resp.text
    assert "paw_auth" in resp.cookies


async def test_challenge_with_wrong_code_401(env) -> None:
    client, _, _ = env
    token = await _get_mfa_token(client)
    resp = await client.post(
        "/api/v1/auth/mfa/challenge", json={"mfa_token": token, "code": "000000"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "mfa_invalid_code"


async def test_challenge_with_invalid_mfa_token_401(env) -> None:
    client, _, _ = env
    resp = await client.post(
        "/api/v1/auth/mfa/challenge",
        json={"mfa_token": "not-a-real-jwt", "code": "000000"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_mfa_token"


async def test_challenge_rejects_wrong_token_type(env) -> None:
    """A token signed with the right secret but missing type=mfa_pending must
    not be accepted as an MFA challenge token (defence against substituting
    the real session JWT)."""
    client, _, _ = env
    bogus = pyjwt.encode(
        {"sub": "deadbeefdeadbeefdeadbeef", "exp": int(time.time()) + 60},
        SECRET,
        algorithm="HS256",
    )
    resp = await client.post(
        "/api/v1/auth/mfa/challenge", json={"mfa_token": bogus, "code": "000000"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_mfa_token"


async def test_challenge_rejects_expired_token(env) -> None:
    client, _, mfa_user_codes = env
    _ = mfa_user_codes
    expired = pyjwt.encode(
        {
            "sub": "deadbeefdeadbeefdeadbeef",
            "type": "mfa_pending",
            "jti": "abc",
            "exp": int(time.time()) - 10,
        },
        SECRET,
        algorithm="HS256",
    )
    resp = await client.post(
        "/api/v1/auth/mfa/challenge", json={"mfa_token": expired, "code": "000000"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_mfa_token"


async def test_challenge_rate_limit_429_after_5_wrong(env) -> None:
    client, _, _ = env
    token = await _get_mfa_token(client)
    for _ in range(5):
        r = await client.post(
            "/api/v1/auth/mfa/challenge", json={"mfa_token": token, "code": "000000"}
        )
        assert r.status_code == 401, r.text
    # 6th attempt — bucket empty.
    r6 = await client.post(
        "/api/v1/auth/mfa/challenge", json={"mfa_token": token, "code": "000000"}
    )
    assert r6.status_code == 429
    assert r6.json()["detail"] == "mfa_too_many_attempts"


async def test_challenge_backup_code_consumed_once(env) -> None:
    client, _, backup_codes = env
    code = backup_codes[0]

    token1 = await _get_mfa_token(client)
    r1 = await client.post("/api/v1/auth/mfa/challenge", json={"mfa_token": token1, "code": code})
    assert r1.status_code in (200, 204), r1.text
    assert "paw_auth" in r1.cookies

    # Second use of the same backup code must fail.
    token2 = await _get_mfa_token(client)
    r2 = await client.post("/api/v1/auth/mfa/challenge", json={"mfa_token": token2, "code": code})
    assert r2.status_code == 401
    assert r2.json()["detail"] == "mfa_invalid_code"
