"""EE auth bridge middleware tests.

Verifies the middleware that bridges fastapi-users JWT auth into the OSS
``require_scope`` system. Concretely:

* admin/owner of the active workspace → ``request.state.full_access``
  becomes True → OSS scope-gated routes return 200.
* member / viewer → ``full_access`` stays False → 403.
* unauthenticated / bad JWT → ``full_access`` stays False → 403.
* OSS API keys (pp_*) and OAuth tokens (ppat_*) → the bridge ignores them
  so the existing OSS AuthMiddleware cascade owns those paths.
"""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("AUTH_SECRET", "test-bridge-secret-do-not-use-in-prod")
os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.ee_auth_bridge import EEAuthBridgeMiddleware
from pocketpaw_ee.cloud.auth.core import SECRET, RevocableJWTStrategy
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

from pocketpaw.api.deps import require_scope

# Every test in this module needs the real fail-closed behaviour — opt out
# of the _TESTING_FULL_ACCESS bypass that the root conftest sets up. Without
# this, the global bypass returns 200 for every caller and the role-based
# acceptance / rejection assertions are meaningless.
pytestmark = pytest.mark.enforce_scope


# ---------------------------------------------------------------------------
# Test app — a single route gated by require_scope("settings:read")
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(EEAuthBridgeMiddleware)

    @app.get(
        "/api/v1/settings",
        dependencies=[Depends(require_scope("settings:read", "settings:write"))],
    )
    async def _settings() -> dict[str, Any]:
        return {"ok": True}

    @app.get(
        "/api/v1/channels",
        dependencies=[Depends(require_scope("channels"))],
    )
    async def _channels() -> dict[str, Any]:
        return {"channels": []}

    return app


# ---------------------------------------------------------------------------
# Test users — one per role on the same active workspace
# ---------------------------------------------------------------------------

_WS_ID = "w-bridge-test"


async def _seed_user(email: str, role: str) -> User:
    user = User(
        email=email,
        hashed_password="x",  # not used; we mint JWTs directly
        is_active=True,
        is_verified=True,
        active_workspace=_WS_ID,
        workspaces=[WorkspaceMembership(workspace=_WS_ID, role=role)],
    )
    await user.insert()
    return user


async def _mint_jwt(user: User) -> str:
    """Mint a real JWT the same way the cloud auth backend does."""
    strategy = RevocableJWTStrategy(secret=SECRET, lifetime_seconds=60)
    return await strategy.write_token(user)


@pytest_asyncio.fixture
async def env(mongo_db):  # noqa: ARG001 — fixture forces Beanie init
    owner = await _seed_user("owner@bridge.test", "owner")
    admin = await _seed_user("admin@bridge.test", "admin")
    member = await _seed_user("member@bridge.test", "member")
    no_ws = User(
        email="lonely@bridge.test",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        active_workspace=None,
        workspaces=[],
    )
    await no_ws.insert()

    tokens = {
        "owner": await _mint_jwt(owner),
        "admin": await _mint_jwt(admin),
        "member": await _mint_jwt(member),
        "no_ws": await _mint_jwt(no_ws),
    }

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, tokens


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_via_cookie_passes_settings_scope(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/settings", cookies={"paw_auth": tokens["owner"]})
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}


@pytest.mark.asyncio
async def test_admin_via_cookie_passes_settings_scope(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/settings", cookies={"paw_auth": tokens["admin"]})
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_admin_via_bearer_passes_channels_scope(env) -> None:
    client, tokens = env
    res = await client.get(
        "/api/v1/channels",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_owner_via_bearer_passes_settings_scope(env) -> None:
    client, tokens = env
    res = await client.get(
        "/api/v1/settings",
        headers={"Authorization": f"Bearer {tokens['owner']}"},
    )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_member_is_rejected_with_403(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/settings", cookies={"paw_auth": tokens["member"]})
    assert res.status_code == 403
    assert "Missing required scope" in res.json()["detail"]


@pytest.mark.asyncio
async def test_member_is_rejected_from_channels_too(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/channels", cookies={"paw_auth": tokens["member"]})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_user_with_no_active_workspace_is_rejected(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/settings", cookies={"paw_auth": tokens["no_ws"]})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_no_token_at_all_is_rejected(env) -> None:
    client, _ = env
    res = await client.get("/api/v1/settings")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_garbage_jwt_is_rejected_silently(env) -> None:
    """A malformed JWT must not crash the bridge — request just continues
    unauthenticated and the scope check rejects it."""
    client, _ = env
    res = await client.get("/api/v1/settings", cookies={"paw_auth": "not.a.real.jwt"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_oss_api_key_bearer_is_passed_through(env) -> None:
    """pp_* bearers must not be decoded by the bridge — they belong to the
    OSS AuthMiddleware cascade. In this isolated app there's no AuthMiddleware,
    so the request falls through to require_scope and 403s, but the assertion
    we care about is that the bridge didn't *accept* it (didn't set
    full_access). 403 is the expected outcome here."""
    client, _ = env
    res = await client.get(
        "/api/v1/settings",
        headers={"Authorization": "Bearer pp_fake_key_value"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_oauth_token_bearer_is_passed_through(env) -> None:
    """Same as above for ppat_* OAuth tokens."""
    client, _ = env
    res = await client.get(
        "/api/v1/settings",
        headers={"Authorization": "Bearer ppat_fake_token_value"},
    )
    assert res.status_code == 403
