"""Tests for TOTP MFA enrollment endpoints (Wave 3 Task 3)."""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pyotp
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import (
    UserCreate,
    UserManager,
    get_user_db,
)
from pocketpaw_ee.cloud.auth.router import router as auth_router

_EMAIL = "mfa-user@example.com"
_PASSWORD = "StrongPass123!"
_WRONG_PASSWORD = "WrongPass456!"


async def _seed_user() -> None:
    async for db in get_user_db():
        manager = UserManager(db)
        await manager.create(
            UserCreate(email=_EMAIL, password=_PASSWORD, is_verified=True),
        )
        return


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def app_client(mongo_db) -> AsyncClient:  # noqa: ARG001 — fixture forces Beanie init
    await _seed_user()
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        login = await client.post(
            "/api/v1/auth/login",
            data={"username": _EMAIL, "password": _PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert login.status_code in (200, 204), login.text
        yield client


def _code_for(secret: str) -> str:
    return pyotp.TOTP(secret).now()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def test_setup_returns_secret_otpauth_and_qr(app_client: AsyncClient) -> None:
    resp = await app_client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["secret"]
    assert body["otpauth_url"].startswith("otpauth://totp/")
    # provisioning_uri URL-encodes the email (@ -> %40).
    assert "mfa-user%40example.com" in body["otpauth_url"]
    assert body["qr_svg"].startswith("<?xml") or body["qr_svg"].startswith("<svg")


async def test_setup_twice_replaces_pending_secret(app_client: AsyncClient) -> None:
    first = (await app_client.post("/api/v1/auth/mfa/setup")).json()
    second = (await app_client.post("/api/v1/auth/mfa/setup")).json()
    assert first["secret"] and second["secret"]
    assert first["secret"] != second["secret"]


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


async def test_verify_with_wrong_code_400(app_client: AsyncClient) -> None:
    await app_client.post("/api/v1/auth/mfa/setup")
    resp = await app_client.post("/api/v1/auth/mfa/verify", json={"code": "000000"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mfa_invalid_code"


async def test_verify_before_setup_400(app_client: AsyncClient) -> None:
    resp = await app_client.post("/api/v1/auth/mfa/verify", json={"code": "123456"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mfa_setup_not_started"


async def test_verify_with_correct_code_enables_and_returns_backup_codes(
    app_client: AsyncClient,
) -> None:
    setup = (await app_client.post("/api/v1/auth/mfa/setup")).json()
    secret = setup["secret"]

    resp = await app_client.post("/api/v1/auth/mfa/verify", json={"code": _code_for(secret)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert isinstance(body["backup_codes"], list)
    assert len(body["backup_codes"]) == 10
    for code in body["backup_codes"]:
        assert "-" in code and len(code) == 9


async def test_verify_when_already_enabled_returns_409(app_client: AsyncClient) -> None:
    setup = (await app_client.post("/api/v1/auth/mfa/setup")).json()
    await app_client.post("/api/v1/auth/mfa/verify", json={"code": _code_for(setup["secret"])})

    again = await app_client.post("/api/v1/auth/mfa/verify", json={"code": "123456"})
    assert again.status_code == 409
    assert again.json()["detail"] == "mfa_already_enabled"


async def test_setup_when_already_enabled_returns_409(app_client: AsyncClient) -> None:
    setup = (await app_client.post("/api/v1/auth/mfa/setup")).json()
    await app_client.post("/api/v1/auth/mfa/verify", json={"code": _code_for(setup["secret"])})

    resp = await app_client.post("/api/v1/auth/mfa/setup")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "mfa_already_enabled"


# ---------------------------------------------------------------------------
# Disable
# ---------------------------------------------------------------------------


async def _enable_mfa(client: AsyncClient) -> str:
    setup = (await client.post("/api/v1/auth/mfa/setup")).json()
    secret = setup["secret"]
    verify = await client.post("/api/v1/auth/mfa/verify", json={"code": _code_for(secret)})
    assert verify.status_code == 200, verify.text
    return secret


async def test_disable_with_wrong_code_keeps_enabled(app_client: AsyncClient) -> None:
    await _enable_mfa(app_client)
    resp = await app_client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": _PASSWORD, "code": "000000"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mfa_invalid_code"


async def test_disable_with_wrong_password_keeps_enabled(app_client: AsyncClient) -> None:
    secret = await _enable_mfa(app_client)
    resp = await app_client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": _WRONG_PASSWORD, "code": _code_for(secret)},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mfa_invalid_password"


async def test_disable_with_correct_password_and_code_disables(
    app_client: AsyncClient,
) -> None:
    secret = await _enable_mfa(app_client)
    resp = await app_client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": _PASSWORD, "code": _code_for(secret)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": False}

    # Setup is now allowed again (state cleared).
    again = await app_client.post("/api/v1/auth/mfa/setup")
    assert again.status_code == 200


async def test_disable_when_not_enabled_returns_400(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": _PASSWORD, "code": "123456"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "mfa_not_enabled"


# ---------------------------------------------------------------------------
# Regenerate backup codes
# ---------------------------------------------------------------------------


async def test_regenerate_replaces_all_codes(app_client: AsyncClient) -> None:
    secret = await _enable_mfa(app_client)
    # The original codes came from verify; we can't read them back since the
    # server only returns them once. Compare regenerate output to a second call.
    first = await app_client.post(
        "/api/v1/auth/mfa/backup-codes/regenerate",
        json={"password": _PASSWORD, "code": _code_for(secret)},
    )
    assert first.status_code == 200, first.text
    codes_a = first.json()["backup_codes"]
    assert len(codes_a) == 10

    second = await app_client.post(
        "/api/v1/auth/mfa/backup-codes/regenerate",
        json={"password": _PASSWORD, "code": _code_for(secret)},
    )
    assert second.status_code == 200
    codes_b = second.json()["backup_codes"]
    assert set(codes_a).isdisjoint(set(codes_b))


async def test_regenerate_when_not_enabled_returns_400(app_client: AsyncClient) -> None:
    resp = await app_client.post(
        "/api/v1/auth/mfa/backup-codes/regenerate",
        json={"password": _PASSWORD, "code": "123456"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Service-level unit tests
# ---------------------------------------------------------------------------


def test_generate_backup_codes_shape() -> None:
    from pocketpaw_ee.cloud.auth import mfa

    plaintext, hashed = mfa.generate_backup_codes()
    assert len(plaintext) == len(hashed) == 10
    for code in plaintext:
        assert len(code) == 9 and code[4] == "-"
    # Hashes are deterministic for the same code (after normalization).
    assert mfa.hash_backup_code(plaintext[0].upper()) == hashed[0]


def test_consume_backup_code_removes_and_returns_true() -> None:
    from pocketpaw_ee.cloud.auth import mfa

    plaintext, hashed = mfa.generate_backup_codes(n=3)

    class _StubUser:
        mfa_backup_codes = list(hashed)

    user = _StubUser()
    assert mfa.consume_backup_code(user, plaintext[1]) is True
    assert len(user.mfa_backup_codes) == 2
    assert mfa.consume_backup_code(user, plaintext[1]) is False


def test_verify_totp_accepts_correct_code() -> None:
    from pocketpaw_ee.cloud.auth import mfa

    secret = "JBSWY3DPEHPK3PXP"
    code = pyotp.TOTP(secret).now()
    assert mfa.verify_totp(secret, code) is True
    assert mfa.verify_totp(secret, "000000") is False
    assert mfa.verify_totp("", code) is False


@pytest.mark.parametrize("issuer", ["PocketPaw", "Custom Co"])
def test_build_otpauth_url(issuer: str) -> None:
    from pocketpaw_ee.cloud.auth import mfa

    secret = "JBSWY3DPEHPK3PXP"
    url = mfa.build_otpauth_url(secret, "u@example.com", issuer=issuer)
    assert url.startswith("otpauth://totp/")
    assert secret in url
    assert "u%40example.com" in url


def test_build_qr_svg_returns_svg_string() -> None:
    from pocketpaw_ee.cloud.auth import mfa

    svg = mfa.build_qr_svg("otpauth://totp/test?secret=JBSWY3DPEHPK3PXP")
    assert isinstance(svg, str)
    assert "<svg" in svg
