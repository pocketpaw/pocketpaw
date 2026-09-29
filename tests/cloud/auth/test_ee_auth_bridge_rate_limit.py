"""Cloud JWT users are exempt from the OSS per-IP api_limiter, and gain nothing else.

The OSS ``AuthMiddleware`` caps every caller it does not itself authenticate
with a per-IP bucket (10 rps, burst 30). A fastapi-users cloud JWT matches none
of its branches, so every logged-in cloud user behind one NAT/IP shared that
bucket and got 429s. The EE bridge now sets ``request.state.ee_user_authenticated``
for an active, non-revoked user and the limiter skips on it.

What must hold, with both middlewares stacked the way ``mount_cloud`` stacks
them (bridge outside, OSS auth inside) and a remote client IP:
  * an authenticated member is not 429'd by a flood from one IP;
  * anonymous, garbage-token, revoked-token and inactive-user floods still are;
  * the flag grants no access: a member still 403s on a require_scope route.
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_SECRET", "test-bridge-secret-do-not-use-in-prod")
os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.ee_auth_bridge import EEAuthBridgeMiddleware
from pocketpaw_ee.cloud.auth.core import SECRET, RevocableJWTStrategy
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

from pocketpaw.api.deps import require_scope
from pocketpaw.dashboard_auth import AuthMiddleware
from pocketpaw.security.rate_limiter import RateLimiter

pytestmark = pytest.mark.enforce_scope

_WS_ID = "w-rl-test"
_FLOOD = 45  # > burst capacity 30


def _build_app() -> FastAPI:
    app = FastAPI()
    # add_middleware is a stack: last added runs outermost, as in mount_cloud.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(EEAuthBridgeMiddleware)

    @app.get("/api/v1/ping")
    async def _ping(request: Request) -> dict[str, Any]:
        return {
            "flag": getattr(request.state, "ee_user_authenticated", False),
            "full_access": getattr(request.state, "full_access", False),
        }

    @app.get(
        "/api/v1/settings",
        dependencies=[Depends(require_scope("settings:read", "settings:write"))],
    )
    async def _settings() -> dict[str, Any]:
        return {"ok": True}

    return app


async def _seed(email: str, *, is_active: bool = True) -> User:
    user = User(
        email=email,
        hashed_password="x",
        is_active=is_active,
        is_verified=True,
        is_superuser=False,
        active_workspace=_WS_ID,
        workspaces=[WorkspaceMembership(workspace=_WS_ID, role="member")],
    )
    await user.insert()
    return user


async def _jwt(user: User) -> str:
    return await RevocableJWTStrategy(secret=SECRET, lifetime_seconds=60).write_token(user)


@pytest_asyncio.fixture
async def env(mongo_db):  # noqa: ARG001 — forces Beanie init
    member = await _seed("member@rl.test")
    inactive = await _seed("inactive@rl.test", is_active=False)
    tokens = {"member": await _jwt(member), "inactive": await _jwt(inactive)}
    # rate=0: no refill, so "45 > capacity 30 -> 429" is deterministic.
    limiter = RateLimiter(rate=0.0, capacity=30)
    # A remote client IP, so the genuine-localhost bypass cannot apply.
    transport = ASGITransport(app=_build_app(), client=("203.0.113.7", 40000))
    with (
        patch("pocketpaw.dashboard_auth.api_limiter", limiter),
        patch("pocketpaw.dashboard_auth.get_access_token", return_value="oss-master-token"),
    ):
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, tokens


async def _flood(client: AsyncClient, **kwargs: Any) -> list[int]:
    return [(await client.get("/api/v1/ping", **kwargs)).status_code for _ in range(_FLOOD)]


@pytest.mark.asyncio
async def test_authenticated_member_flood_is_not_rate_limited(env) -> None:
    client, tokens = env
    codes = await _flood(client, cookies={"paw_auth": tokens["member"]})
    assert 429 not in codes, codes
    res = await client.get("/api/v1/ping", headers={"Authorization": f"Bearer {tokens['member']}"})
    assert res.json() == {"flag": True, "full_access": False}


@pytest.mark.asyncio
async def test_anonymous_flood_is_still_limited(env) -> None:
    client, _ = env
    assert 429 in await _flood(client)


@pytest.mark.asyncio
async def test_garbage_jwt_flood_is_still_limited(env) -> None:
    client, _ = env
    assert 429 in await _flood(client, cookies={"paw_auth": "not.a.jwt"})


@pytest.mark.asyncio
async def test_revoked_jwt_flood_is_still_limited(env) -> None:
    client, tokens = env
    with patch(
        "pocketpaw_ee.cloud.auth.sessions.is_revoked", new=AsyncMock(return_value=True)
    ) as revoked:
        codes = await _flood(client, cookies={"paw_auth": tokens["member"]})
    assert revoked.await_count > 0  # the revocation path was really exercised
    assert 429 in codes


@pytest.mark.asyncio
async def test_inactive_user_flood_is_still_limited(env) -> None:
    client, tokens = env
    assert 429 in await _flood(client, cookies={"paw_auth": tokens["inactive"]})


@pytest.mark.asyncio
async def test_flag_grants_no_scope_access(env) -> None:
    """A non-superuser with the limiter flag still 403s on a scope-gated route."""
    client, tokens = env
    res = await client.get("/api/v1/settings", cookies={"paw_auth": tokens["member"]})
    assert res.status_code == 403, res.text


@pytest.mark.asyncio
async def test_flag_does_not_pass_the_oss_401_gate(env) -> None:
    """Outside the auth-optional /api/v1/ prefix the OSS middleware still 401s."""
    client, tokens = env
    res = await client.get("/internal/anything", cookies={"paw_auth": tokens["member"]})
    assert res.status_code == 401, res.text
