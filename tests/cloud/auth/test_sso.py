"""Tests for OIDC SSO config + login flow (Wave 3 Task 10)."""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")
os.environ.setdefault(
    "POCKETPAW_SSO_REDIRECT_URI",
    "http://localhost:8888/api/v1/auth/sso/callback",
)

import fakeredis.aioredis
import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.auth.sso import crypto, oidc
from pocketpaw_ee.cloud.auth.sso import service as sso_service
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace

_ADMIN_EMAIL = "admin@acme.com"
_ADMIN_PASSWORD = "StrongPass123!"
_WS_SLUG = "acme"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_admin_and_workspace() -> tuple[str, str]:
    async for db in get_user_db():
        manager = UserManager(db)
        admin = await manager.create(UserCreate(email=_ADMIN_EMAIL, password=_ADMIN_PASSWORD))
        break
    ws = Workspace(name="Acme", slug=_WS_SLUG, owner=str(admin.id))
    await ws.insert()
    admin.workspaces.append(
        WorkspaceMembership(workspace=str(ws.id), role="owner"),
    )
    admin.active_workspace = str(ws.id)
    await admin.save()
    return str(admin.id), str(ws.id)


@pytest_asyncio.fixture
async def env(mongo_db, monkeypatch):  # noqa: ARG001
    crypto._reset_for_tests()
    oidc._clear_discovery_cache()
    admin_id, ws_id = await _seed_admin_and_workspace()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # Login as admin to get the cookie for admin routes
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code in (200, 204), resp.text
        yield client, admin_id, ws_id, fake


# ---------------------------------------------------------------------------
# Crypto + service unit tests
# ---------------------------------------------------------------------------


def test_crypto_roundtrip(monkeypatch):
    crypto._reset_for_tests()
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.delenv("POCKETPAW_SSO_ENCRYPTION_KEY", raising=False)
    ct = crypto.encrypt("sso-client-secret")
    assert ct != "sso-client-secret"
    assert crypto.decrypt(ct) == "sso-client-secret"


@pytest.mark.asyncio
async def test_upsert_encrypts_secret(env):
    _client, _admin_id, ws_id, _fake = env
    cfg = await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )
    assert cfg.client_secret_encrypted != "super-secret"
    assert crypto.decrypt(cfg.client_secret_encrypted) == "super-secret"

    # Round-trip from doc.
    ws = await Workspace.get(PydanticObjectId(ws_id))
    assert ws is not None
    assert ws.sso_config is not None
    assert crypto.decrypt(ws.sso_config.client_secret_encrypted) == "super-secret"


# ---------------------------------------------------------------------------
# Login flow — happy path
# ---------------------------------------------------------------------------


_DISCOVERY = {
    "authorization_endpoint": "https://acme.okta.com/oauth2/v1/authorize",
    "token_endpoint": "https://acme.okta.com/oauth2/v1/token",
    "userinfo_endpoint": "https://acme.okta.com/oauth2/v1/userinfo",
    "jwks_uri": "https://acme.okta.com/oauth2/v1/keys",
}


async def _fake_discover(issuer, provider_key):
    return _DISCOVERY


async def _fake_exchange_code(
    token_endpoint,
    code,
    client_id,
    client_secret,
    redirect_uri,
    *,
    code_verifier=None,
):
    return {"id_token": "fake.id.token", "access_token": "fake-access-token"}


async def _fake_parse(id_token, jwks_uri, *, audience, issuer, nonce=None):
    return {
        "email": "newuser@acme.com",
        "name": "New User",
        "aud": audience,
        "iss": issuer,
        "nonce": nonce,
    }


async def _fake_userinfo(userinfo_endpoint, access_token):
    return {"email": "newuser@acme.com", "name": "New User", "email_verified": True}


@pytest.mark.asyncio
async def test_begin_login_stores_state_and_returns_authorize_url(env, monkeypatch):
    _client, _admin_id, ws_id, fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )
    monkeypatch.setattr(oidc, "discover", _fake_discover)

    url = await sso_service.begin_login(_WS_SLUG)
    assert url.startswith("https://acme.okta.com/oauth2/v1/authorize?")
    assert "code_challenge=" in url
    assert "state=" in url

    # The state row in Redis carries the workspace_id binding.
    state = url.split("state=")[1].split("&")[0]
    raw = await fake.get(f"sso_state:{state}")
    assert raw is not None
    import json

    payload = json.loads(raw)
    assert payload["workspace_id"] == ws_id
    assert "code_verifier" in payload
    assert "nonce" in payload
    # Nonce must also ride the authorize URL so the provider echoes it.
    assert f"nonce={payload['nonce']}" in url


@pytest.mark.asyncio
async def test_complete_login_jit_provisions(env, monkeypatch):
    _client, _admin_id, ws_id, fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )
    monkeypatch.setattr(oidc, "discover", _fake_discover)
    monkeypatch.setattr(oidc, "exchange_code", _fake_exchange_code)
    monkeypatch.setattr(oidc, "parse_id_token", _fake_parse)
    monkeypatch.setattr(oidc, "fetch_userinfo", _fake_userinfo)

    # Seed a state row that points at the workspace.
    import json

    await fake.setex(
        "sso_state:abc123",
        600,
        json.dumps({"workspace_id": ws_id, "code_verifier": "v", "nonce": None}),
    )
    user = await sso_service.complete_login("auth-code", "abc123")
    assert user.email == "newuser@acme.com"
    # Sentinel — never plaintext-empty (that risked verifier-treats-as-OK).
    assert user.hashed_password.startswith("!sso-only-")
    assert len(user.hashed_password) > 32
    assert user.is_verified is True
    assert any(m.workspace == ws_id and m.role == "member" for m in user.workspaces)

    # State was one-shot consumed.
    assert await fake.get("sso_state:abc123") is None


@pytest.mark.asyncio
async def test_complete_login_nonce_mismatch_rejects(env, monkeypatch):
    _client, _admin_id, ws_id, fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )
    monkeypatch.setattr(oidc, "discover", _fake_discover)
    monkeypatch.setattr(oidc, "exchange_code", _fake_exchange_code)
    monkeypatch.setattr(oidc, "fetch_userinfo", _fake_userinfo)

    # Token claims carry the wrong nonce — must reject before any user write.
    async def _bad_nonce_parse(id_token, jwks_uri, *, audience, issuer, nonce=None):
        import jwt as _pyjwt

        raise _pyjwt.InvalidTokenError("nonce mismatch")

    monkeypatch.setattr(oidc, "parse_id_token", _bad_nonce_parse)

    import json

    await fake.setex(
        "sso_state:nonce-test",
        600,
        json.dumps({"workspace_id": ws_id, "code_verifier": "v", "nonce": "expected"}),
    )
    with pytest.raises(Exception):  # pyjwt.InvalidTokenError bubbles
        await sso_service.complete_login("auth-code", "nonce-test")


@pytest.mark.asyncio
async def test_complete_login_state_mismatch_rejects(env):
    _client, _admin_id, _ws_id, _fake = env
    with pytest.raises(Forbidden):
        await sso_service.complete_login("auth-code", "nope-no-state")


@pytest.mark.asyncio
async def test_complete_login_domain_mismatch_no_jit(env, monkeypatch):
    _client, _admin_id, ws_id, fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],  # outsider.com is NOT here
    )
    monkeypatch.setattr(oidc, "discover", _fake_discover)
    monkeypatch.setattr(oidc, "exchange_code", _fake_exchange_code)

    async def _outsider_parse(id_token, jwks_uri, *, audience, issuer, nonce=None):
        return {
            "email": "stranger@outsider.com",
            "aud": audience,
            "iss": issuer,
            "nonce": nonce,
        }

    async def _outsider_userinfo(userinfo_endpoint, access_token):
        return {"email": "stranger@outsider.com"}

    monkeypatch.setattr(oidc, "parse_id_token", _outsider_parse)
    monkeypatch.setattr(oidc, "fetch_userinfo", _outsider_userinfo)

    import json

    await fake.setex(
        "sso_state:xyz",
        600,
        json.dumps({"workspace_id": ws_id, "code_verifier": "v", "nonce": None}),
    )
    with pytest.raises(Forbidden) as exc_info:
        await sso_service.complete_login("auth-code", "xyz")
    assert exc_info.value.code == "sso.domain_not_allowed"

    # User was NOT created.
    assert await User.find_one(User.email == "stranger@outsider.com") is None


@pytest.mark.asyncio
async def test_complete_login_existing_user_foreign_domain_rejected(env, monkeypatch):
    """Pre-existing user (any other workspace) cannot squat in via SSO.

    The earlier code unconditionally `_ensure_membership`'d any existing
    user; an attacker who controlled the IdP identity for a user that
    happened to exist in our DB could join the SSO-enabled workspace
    even when their email domain was nowhere near the allowlist. The
    gate now requires membership-of-this-workspace OR domain-in-allowlist.
    """
    _client, _admin_id, ws_id, fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )

    # Seed a foreign user that is NOT a member of ws_id.
    foreign = User(
        email="foreign@outsider.com",
        hashed_password="x",
        is_active=True,
        is_verified=True,
    )
    await foreign.insert()

    monkeypatch.setattr(oidc, "discover", _fake_discover)
    monkeypatch.setattr(oidc, "exchange_code", _fake_exchange_code)

    async def _foreign_parse(id_token, jwks_uri, *, audience, issuer, nonce=None):
        return {
            "email": "foreign@outsider.com",
            "aud": audience,
            "iss": issuer,
            "nonce": nonce,
        }

    async def _foreign_userinfo(userinfo_endpoint, access_token):
        return {"email": "foreign@outsider.com"}

    monkeypatch.setattr(oidc, "parse_id_token", _foreign_parse)
    monkeypatch.setattr(oidc, "fetch_userinfo", _foreign_userinfo)

    import json

    await fake.setex(
        "sso_state:foreign",
        600,
        json.dumps({"workspace_id": ws_id, "code_verifier": "v", "nonce": None}),
    )

    with pytest.raises(Forbidden) as exc_info:
        await sso_service.complete_login("auth-code", "foreign")
    assert exc_info.value.code == "sso.domain_not_allowed"

    # And the user was NOT silently added to ws_id.
    refetched = await User.find_one(User.email == "foreign@outsider.com")
    assert refetched is not None
    assert all(m.workspace != ws_id for m in refetched.workspaces)


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_connection_happy_path(env, monkeypatch):
    _client, _admin_id, ws_id, _fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )
    monkeypatch.setattr(oidc, "discover", _fake_discover)
    result = await sso_service.test_connection(ws_id)
    assert result["ok"] is True
    assert result["issuer"] == "https://acme.okta.com"
    assert "token_endpoint" in result["endpoints"]


@pytest.mark.asyncio
async def test_test_connection_bad_discovery_returns_error(env, monkeypatch):
    _client, _admin_id, ws_id, _fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="generic_oidc",
        issuer="https://bad.example.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )

    async def _bad_discover(issuer, provider_key):
        import httpx

        raise httpx.ConnectError("dns")

    monkeypatch.setattr(oidc, "discover", _bad_discover)
    result = await sso_service.test_connection(ws_id)
    assert result["ok"] is False
    assert "discovery_failed" in result["error"]


# ---------------------------------------------------------------------------
# Router-level: admin role required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_cannot_upsert_sso(env):
    client, _admin_id, ws_id, _fake = env
    # Create a plain member account on a fresh client (no admin cookie).
    plain = _build_app()
    transport = ASGITransport(app=plain)
    async with AsyncClient(transport=transport, base_url="http://t") as member_client:
        resp = await member_client.post(
            "/api/v1/auth/register",
            json={"email": "member@acme.com", "password": "StrongPass123!"},
        )
        assert resp.status_code in (200, 201), resp.text
        # Attach as member of the workspace.
        member = await User.find_one(User.email == "member@acme.com")
        assert member is not None
        member.workspaces.append(WorkspaceMembership(workspace=ws_id, role="member"))
        member.active_workspace = ws_id
        await member.save()

        await member_client.post(
            "/api/v1/auth/login",
            data={"username": "member@acme.com", "password": "StrongPass123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        resp = await member_client.post(
            f"/api/v1/workspaces/{ws_id}/sso",
            json={
                "provider": "okta",
                "issuer": "https://acme.okta.com",
                "client_id": "x",
                "client_secret": "y",
                "allowed_domains": ["acme.com"],
            },
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_upsert_returns_masked_secret(env):
    client, _admin_id, ws_id, _fake = env
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sso",
        json={
            "provider": "okta",
            "issuer": "https://acme.okta.com",
            "client_id": "client-123",
            "client_secret": "super-secret",
            "allowed_domains": ["acme.com"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_secret"] == "***"
    assert body["client_id"] == "client-123"

    # GET round-trip — still masked.
    resp = await client.get(f"/api/v1/workspaces/{ws_id}/sso")
    assert resp.status_code == 200
    assert resp.json()["client_secret"] == "***"


@pytest.mark.asyncio
async def test_delete_sso(env):
    client, _admin_id, ws_id, _fake = env
    await sso_service.upsert_sso_config(
        ws_id,
        provider="okta",
        issuer="https://acme.okta.com",
        client_id="client-123",
        client_secret_plain="super-secret",
        allowed_domains=["acme.com"],
    )
    resp = await client.delete(f"/api/v1/workspaces/{ws_id}/sso")
    assert resp.status_code == 204
    assert await sso_service.get_sso_config(ws_id) is None
