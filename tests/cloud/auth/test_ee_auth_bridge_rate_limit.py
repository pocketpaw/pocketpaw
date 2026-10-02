"""Signed-in cloud users skip the OSS per-IP api_limiter, and gain nothing else.

The OSS ``AuthMiddleware`` caps every caller it does not itself authenticate
with a per-IP bucket (10 rps, burst 30). A fastapi-users cloud JWT matches none
of its branches, and one reload of the static web client fires 20+ /api/v1/
calls, so any bucket that size 429s a signed-in user. The EE bridge sets
``request.state.ee_user_authenticated`` for an active, non-revoked user
(guests included) and the limiter skips on it. Guest minting is capped
separately by ``guest_mint_limiter``.

What must hold, with both middlewares stacked the way ``mount_cloud`` stacks
them (bridge outside, OSS auth inside) and a remote client IP:
  * a member or guest is never 429'd by a burst from one IP, and its traffic
    does not drain the per-IP bucket anonymous callers share;
  * anonymous, garbage-token, revoked, inactive and deleted-user floods are
    still 429'd on the per-IP bucket;
  * the flag grants no access: a member still 403s on a require_scope route
    and 401s outside the auth-optional prefix.
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


async def _seed(email: str, *, is_active: bool = True, is_guest: bool = False) -> User:
    user = User(
        email=email,
        hashed_password="x",
        is_active=is_active,
        is_verified=not is_guest,
        is_guest=is_guest,
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
    other = await _seed("other@rl.test")
    guest = await _seed("guest-abc@rl.test", is_guest=True)
    inactive = await _seed("inactive@rl.test", is_active=False)
    deleted = await _seed("deleted@rl.test")
    tokens = {
        "member": await _jwt(member),
        "other": await _jwt(other),
        "guest": await _jwt(guest),
        "inactive": await _jwt(inactive),
        "deleted": await _jwt(deleted),
    }
    await deleted.delete()  # a still-valid JWT for a user that no longer exists
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


def _as(tokens: dict[str, str], who: str) -> dict[str, Any]:
    return {"cookies": {"paw_auth": tokens[who]}}


@pytest.mark.asyncio
async def test_member_flood_is_not_limited_and_leaves_the_ip_bucket_alone(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/ping", headers={"Authorization": f"Bearer {tokens['member']}"})
    assert res.json() == {"flag": True, "full_access": False}
    codes = await _flood(client, **_as(tokens, "member"))
    assert codes.count(429) == 0, codes
    # The member drew nothing from the shared IP bucket: an anonymous caller
    # on the SAME IP still has its full 30.
    anon = await _flood(client)
    assert anon.count(200) == 30, anon


@pytest.mark.asyncio
async def test_guest_flood_is_not_limited(env) -> None:
    """Guests are signed-in users too; minting them is what guest_mint_limiter caps."""
    client, tokens = env
    res = await client.get("/api/v1/ping", **_as(tokens, "guest"))
    assert res.json() == {"flag": True, "full_access": False}
    codes = await _flood(client, **_as(tokens, "guest"))
    assert codes.count(429) == 0, codes


@pytest.mark.asyncio
async def test_anonymous_flood_does_not_block_a_signed_in_user(env) -> None:
    client, tokens = env
    assert 429 in await _flood(client)
    assert (await client.get("/api/v1/ping", **_as(tokens, "member"))).status_code == 200


@pytest.mark.asyncio
async def test_deleted_user_jwt_stays_on_the_ip_bucket(env) -> None:
    client, tokens = env
    res = await client.get("/api/v1/ping", **_as(tokens, "deleted"))
    assert res.json() == {"flag": False, "full_access": False}
    assert 429 in await _flood(client, **_as(tokens, "deleted"))
    # It drained the shared IP bucket, so plain anonymous traffic is 429 too.
    assert (await client.get("/api/v1/ping")).status_code == 429


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
    # On the shared IP bucket, not a bucket of its own.
    assert (await client.get("/api/v1/ping")).status_code == 429


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


@pytest.mark.asyncio
async def test_member_double_reload_burst_is_not_throttled(env) -> None:
    """One reload of the static web client fires 20+ /api/v1/ calls; two back
    to back must not 429 (``{"detail": "Too many requests"}``)."""
    client, tokens = env
    codes = [
        (await client.get("/api/v1/ping", **_as(tokens, "member"))).status_code for _ in range(60)
    ]
    assert codes.count(429) == 0, codes
