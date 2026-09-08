"""The EE auth bridge stamps the session's tenant for OSS-package routers.

This is a seam, not a convenience. ``src/pocketpaw/api/v1/cloud_projects.py``
stores per-tenant data under ``projects/{workspace_id}/...`` but lives in the
OSS package, which cannot import ``pocketpaw_ee`` — an import-linter contract
forbids it. With no way to learn the tenant it read one out of an
``X-Workspace-Id`` header, which the caller chooses, so every workspace's
project storage was reachable by anyone who named it.

``request.state.workspace_id`` is how an OSS router gets an AUTHENTICATED
tenant. Nothing else on ``request.state`` carries one: ``full_access`` is a
boolean and is reserved for platform superusers, and ``api_key`` / ``oauth_token``
have no workspace field at all.

The property that matters, and the one a reasonable-looking change breaks: it is
stamped for EVERY resolved user, not only superusers. The full_access grant
immediately below it is deliberately superuser-only (the 2026-06-10 escalation
fix), and it would be an easy and wrong tidy-up to fold these into that branch.
An ordinary workspace member is exactly the caller who needs their own workspace
resolved — they are who uses the /code surface.

Mutations that must fail these tests: not stamping the workspace, stamping it
only for superusers, and stamping it from a request header.
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_SECRET", "test-bridge-secret-do-not-use-in-prod")
os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.ee_auth_bridge import EEAuthBridgeMiddleware
from pocketpaw_ee.cloud.auth.core import SECRET, RevocableJWTStrategy
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

_WS_ID = "w-stamp-test"


def _build_app() -> FastAPI:
    """An app whose one route reports what the bridge put on request.state."""
    app = FastAPI()
    app.add_middleware(EEAuthBridgeMiddleware)

    @app.get("/api/v1/whoami")
    async def _whoami(request: Request) -> dict:
        return {
            "workspace_id": getattr(request.state, "workspace_id", None),
            "user_id": getattr(request.state, "user_id", None),
            "full_access": getattr(request.state, "full_access", False),
        }

    return app


async def _seed(email: str, role: str, *, is_superuser: bool = False) -> User:
    user = User(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        is_superuser=is_superuser,
        active_workspace=_WS_ID,
        workspaces=[WorkspaceMembership(workspace=_WS_ID, role=role)],
    )
    await user.insert()
    return user


async def _mint(user: User) -> str:
    strategy = RevocableJWTStrategy(secret=SECRET, lifetime_seconds=60)
    return await strategy.write_token(user)


@pytest_asyncio.fixture
async def env(mongo_db):  # noqa: ARG001 — forces Beanie init
    member = await _seed("member@stamp.test", "member")
    admin = await _seed("root@stamp.test", "owner", is_superuser=True)
    tokens = {"member": await _mint(member), "admin": await _mint(admin)}
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://t"
    ) as client:
        yield client, tokens, member


@pytest.mark.asyncio
async def test_an_ordinary_member_gets_their_workspace_stamped(env) -> None:
    """The case the seam exists for, and the one a superuser-only stamp breaks."""
    client, tokens, member = env
    res = await client.get("/api/v1/whoami", cookies={"paw_auth": tokens["member"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["workspace_id"] == _WS_ID
    assert body["user_id"] == str(member.id)
    # Unchanged by this seam: an ordinary member is still not a platform admin.
    assert body["full_access"] is False


@pytest.mark.asyncio
async def test_a_header_does_not_influence_the_stamp(env) -> None:
    """The tenant comes from the JWT, so naming another workspace changes nothing."""
    client, tokens, _ = env
    res = await client.get(
        "/api/v1/whoami",
        cookies={"paw_auth": tokens["member"]},
        headers={"X-Workspace-Id": "w-victim"},
    )
    assert res.json()["workspace_id"] == _WS_ID


@pytest.mark.asyncio
async def test_an_unauthenticated_request_is_stamped_with_nothing(env) -> None:
    """No session means no tenant, which is what makes the OSS side fail closed."""
    client, _, _ = env
    res = await client.get("/api/v1/whoami", headers={"X-Workspace-Id": "w-victim"})
    assert res.json()["workspace_id"] is None


@pytest.mark.asyncio
async def test_a_superuser_is_stamped_too_and_still_gets_full_access(env) -> None:
    client, tokens, _ = env
    body = (await client.get("/api/v1/whoami", cookies={"paw_auth": tokens["admin"]})).json()
    assert body["workspace_id"] == _WS_ID
    assert body["full_access"] is True
