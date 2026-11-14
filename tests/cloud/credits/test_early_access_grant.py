# tests/cloud/credits/test_early_access_grant.py: the one-time early-access
# grant. A verified user gets ``early_access_grant_credits`` exactly once, into a
# workspace they OWN, through every door that calls ``grant_early_access``:
# social sign-in (including pre-launch accounts that already own a workspace),
# first owned-workspace create and the guest social upgrade. A failed grant
# never fails sign-in or workspace create. Also pins the launch switch that
# closes password sign-up (``password_signup_open``).
#
# Nothing on the money path is faked: the real ledger, the real workspace
# create and the real guest mint run against mongomock. Only infrastructure is
# stubbed: the realtime audience resolver (exists only after init_realtime),
# the provider key check on guest mint, and Redis for the auth router.
from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import MagicMock

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

GRANT = 1_000


@pytest.fixture(autouse=True)
def _infra(monkeypatch):
    from cryptography.fernet import Fernet
    from pocketpaw_ee.cloud.byok import service as byok_service

    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: MagicMock())
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())

    async def _ok(api_key, *, provider="anthropic", base_url=None, model=None):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(byok_service, "validate_key", _ok)


async def _user(*, verified: bool = True):
    from pocketpaw_ee.cloud.models.user import User

    user = User(
        email=f"ea-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        is_verified=verified,
    )
    await user.insert()
    return user


async def _create_ws(user) -> str:  # noqa: ANN001
    from pocketpaw_ee.cloud.models.user import User
    from pocketpaw_ee.cloud.workspace import service as workspace_service
    from pocketpaw_ee.cloud.workspace.dto import CreateWorkspaceRequest

    fresh = await User.get(user.id)
    tag = uuid.uuid4().hex[:8]
    ws = await workspace_service.create(
        workspace_service.legacy_ctx(fresh),
        CreateWorkspaceRequest(name=f"WS {tag}", slug=f"ws-{tag}"),
    )
    return ws.id


async def _grant_entries(user_id: str) -> list:
    from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

    return await CreditLedgerEntry.find({"idempotency_key": f"early_access:{user_id}"}).to_list()


async def _balance(workspace_id: str) -> int:
    from pocketpaw_ee.cloud.credits import service as credits_service

    return await credits_service.balance(workspace_id)


async def _reload(user):  # noqa: ANN001
    from pocketpaw_ee.cloud.models.user import User

    return await User.get(user.id)


async def test_first_owned_workspace_gets_the_grant_once(mongo_db):  # noqa: ARG001
    user = await _user()
    ws1 = await _create_ws(user)

    assert await _balance(ws1) == GRANT
    entries = await _grant_entries(str(user.id))
    assert len(entries) == 1
    assert entries[0].kind == "genesis"
    assert entries[0].workspace == ws1
    fresh = await _reload(user)
    assert fresh.early_access_workspace_id == ws1
    assert fresh.early_access_granted_at is not None


async def test_a_second_owned_workspace_gets_no_second_grant(mongo_db):  # noqa: ARG001
    user = await _user()
    ws1 = await _create_ws(user)
    ws2 = await _create_ws(user)

    assert await _balance(ws1) == GRANT
    assert await _balance(ws2) == 0
    assert len(await _grant_entries(str(user.id))) == 1
    assert (await _reload(user)).early_access_workspace_id == ws1


async def test_an_invited_users_grant_never_lands_in_the_inviters_workspace(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.user import WorkspaceMembership

    inviter = await _user()
    inviter_ws = await _create_ws(inviter)

    invitee = await _user()
    invitee.workspaces.append(WorkspaceMembership(workspace=inviter_ws, role="member"))
    invitee.active_workspace = inviter_ws
    await invitee.save()

    # A member, not an owner: nothing to grant into yet, and nothing claimed.
    await credits_service.grant_early_access(await _reload(invitee))
    assert await _balance(inviter_ws) == GRANT
    assert await _grant_entries(str(invitee.id)) == []
    assert (await _reload(invitee)).early_access_workspace_id is None

    own_ws = await _create_ws(invitee)
    assert await _balance(own_ws) == GRANT
    assert await _balance(inviter_ws) == GRANT
    entries = await _grant_entries(str(invitee.id))
    assert [e.workspace for e in entries] == [own_ws]


async def test_an_unverified_user_gets_no_grant(mongo_db):  # noqa: ARG001
    user = await _user(verified=False)
    ws = await _create_ws(user)

    assert await _balance(ws) == 0
    assert await _grant_entries(str(user.id)) == []
    fresh = await _reload(user)
    assert fresh.early_access_workspace_id is None
    assert fresh.early_access_granted_at is None


async def test_a_retry_after_a_crash_before_the_stamp_does_not_grant_twice(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.credits import service as credits_service

    user = await _user()
    ws = await _create_ws(user)

    # Simulate a crash between the ledger grant and the granted_at stamp: the
    # stamp is gone, so the fast path cannot short-circuit, and only the ledger
    # idempotency key stands between a retry and a second grant.
    fresh = await _reload(user)
    fresh.early_access_granted_at = None
    await fresh.save()

    await credits_service.grant_early_access(await _reload(user))
    await credits_service.grant_early_access(await _reload(user))

    assert await _balance(ws) == GRANT
    assert len(await _grant_entries(str(user.id))) == 1
    assert (await _reload(user)).early_access_granted_at is not None


async def test_concurrent_grants_land_once(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.user import User
    from pocketpaw_ee.cloud.models.workspace import Workspace

    user = await _user()
    # Two owned workspaces and no claim yet: concurrent callers must agree on
    # ONE of them, or the per-workspace ledger key would let both land.
    for tag in ("a", "b"):
        await Workspace(name=tag, slug=f"{tag}-{uuid.uuid4().hex[:6]}", owner=str(user.id)).insert()

    u1, u2 = await User.get(user.id), await User.get(user.id)
    await asyncio.gather(
        credits_service.grant_early_access(u1), credits_service.grant_early_access(u2)
    )

    entries = await _grant_entries(str(user.id))
    assert len(entries) == 1
    assert (await _reload(user)).early_access_workspace_id == entries[0].workspace


async def test_social_guest_upgrade_grants_exactly_once(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.auth import guest as guest_service
    from pocketpaw_ee.cloud.credits import service as credits_service

    guest = await guest_service.mint_guest("sk-ant-" + "x" * 40)
    guest_ws = guest.active_workspace
    # A guest is unverified: minting its notebook granted nothing.
    assert await _balance(guest_ws) == 0

    upgraded = await guest_service.upgrade_guest_via_social(
        guest, email=f"g-{uuid.uuid4().hex[:6]}@example.com"
    )
    assert await _balance(guest_ws) == GRANT

    # Any later door (another workspace, a repeat call) is a no-op.
    await credits_service.grant_early_access(await _reload(upgraded))
    await _create_ws(upgraded)
    assert await _balance(guest_ws) == GRANT
    assert len(await _grant_entries(str(upgraded.id))) == 1


async def test_free_monthly_ceiling_is_at_least_the_grant():
    from pocketpaw_ee.cloud.billing.plans import get_plan

    from pocketpaw.config import Settings

    default = Settings.model_fields["early_access_grant_credits"].default
    assert default == GRANT
    assert get_plan("free").monthly_ceiling >= default


# ---------------------------------------------------------------------------
# Launch switch: password sign-up closed
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_app(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth.router import guest_mint_limiter
    from pocketpaw_ee.cloud.auth.router import router as auth_router

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(guest_mint_limiter, "allow", lambda _ip: True)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


def _signup_open(monkeypatch, value: bool) -> None:
    from pocketpaw_ee.cloud.auth import router as auth_router_mod

    from pocketpaw.config import get_settings

    settings = get_settings().model_copy(update={"password_signup_open": value})
    monkeypatch.setattr(auth_router_mod, "get_settings", lambda: settings)


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_register_is_refused_when_password_signup_is_closed(auth_app, monkeypatch):
    from pocketpaw_ee.cloud.models.user import User

    _signup_open(monkeypatch, False)
    email = f"closed-{uuid.uuid4().hex[:6]}@example.com"
    async with _client(auth_app) as c:
        resp = await c.post(
            "/api/v1/auth/register", json={"email": email, "password": "StrongPass123!"}
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "auth.password_signup_closed"
    assert await User.find_one(User.email == email) is None


async def test_register_works_when_password_signup_is_open(auth_app, monkeypatch):
    _signup_open(monkeypatch, True)
    async with _client(auth_app) as c:
        resp = await c.post(
            "/api/v1/auth/register",
            json={
                "email": f"open-{uuid.uuid4().hex[:6]}@example.com",
                "password": "StrongPass123!",
            },
        )
    assert resp.status_code == 201, resp.text


async def test_guest_password_upgrade_is_refused_when_closed(auth_app, monkeypatch):
    _signup_open(monkeypatch, False)
    async with _client(auth_app) as c:
        minted = await c.post("/api/v1/auth/guest", json={"api_key": "sk-ant-" + "x" * 40})
        assert minted.status_code in (200, 204), minted.text
        resp = await c.post(
            "/api/v1/auth/guest/upgrade",
            json={"email": f"g-{uuid.uuid4().hex[:6]}@example.com", "password": "StrongPass123!"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "auth.password_signup_closed"


async def test_wiped_claim_and_deleted_workspace_cannot_farm_a_second_grant(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.workspace import Workspace

    user = await _user()
    ws1 = await _create_ws(user)

    # A stale User.save() elsewhere overwrites both claim fields with None, and
    # the user then deletes the granted workspace and creates a fresh one.
    fresh = await _reload(user)
    fresh.early_access_workspace_id = None
    fresh.early_access_granted_at = None
    await fresh.save()
    doc = await Workspace.get(ws1)
    doc.deleted_at = doc.createdAt
    await doc.save()

    ws2 = await _create_ws(user)
    await credits_service.grant_early_access(await _reload(user))

    assert await _balance(ws2) == 0
    assert len(await _grant_entries(str(user.id))) == 1
    healed = await _reload(user)
    assert healed.early_access_workspace_id == ws1
    assert healed.early_access_granted_at is not None


# ---------------------------------------------------------------------------
# Social sign-in door, and best-effort grants
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def social_app(auth_app, monkeypatch):
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420")
    monkeypatch.setenv(
        "POCKETPAW_SOCIAL_REDIRECT_URI", "http://localhost:8888/api/v1/auth/social/callback"
    )
    return auth_app


async def _social_login(app: FastAPI, monkeypatch, email: str, account_id: str) -> None:  # noqa: ANN001
    """One full web sign-in through the callback. The login branch is
    provider-agnostic, so GitHub stands in for Google here."""
    from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity
    from pocketpaw_ee.cloud.auth.social.providers.github import GitHubProvider

    identity = SocialIdentity(provider="github", account_id=account_id, email=email)

    async def fake_exchange(self, **kwargs):  # noqa: ANN001, ARG001
        return identity

    monkeypatch.setattr(GitHubProvider, "exchange", fake_exchange)
    async with _client(app) as c:
        start = await c.get(
            "/api/v1/auth/social/github/login", params={"flow": "web"}, follow_redirects=False
        )
        state = start.headers["location"].split("state=")[1].split("&")[0]
        back = await c.get(
            "/api/v1/auth/social/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
    assert back.status_code == 302, back.text
    assert "paw_auth" in back.cookies


async def _prelaunch_account():
    """A verified user who already owns a workspace that was never granted
    (created before launch), so only the sign-in door can grant it."""
    from pocketpaw_ee.cloud.models.workspace import Workspace

    user = await _user()
    ws = Workspace(name="pre", slug=f"pre-{uuid.uuid4().hex[:6]}", owner=str(user.id))
    await ws.insert()
    return user, str(ws.id)


async def test_social_sign_in_grants_an_existing_owner_exactly_once(social_app, monkeypatch):
    user, ws = await _prelaunch_account()
    assert await _balance(ws) == 0

    # First Google sign-in attaches by verified email ("link"), the second
    # finds the attached identity ("sign_in"). Only the first grants.
    await _social_login(social_app, monkeypatch, user.email, "gh-prelaunch")
    assert await _balance(ws) == GRANT
    assert len(await _grant_entries(str(user.id))) == 1
    granted_at = (await _reload(user)).early_access_granted_at
    assert granted_at is not None

    await _social_login(social_app, monkeypatch, user.email, "gh-prelaunch")
    assert await _balance(ws) == GRANT
    assert len(await _grant_entries(str(user.id))) == 1
    assert (await _reload(user)).early_access_granted_at == granted_at


async def test_a_failed_grant_fails_neither_create_nor_sign_in(social_app, monkeypatch):
    from pocketpaw_ee.cloud.credits import service as credits_service

    real_grant = credits_service.grant

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("ledger down")

    monkeypatch.setattr(credits_service, "grant", _boom)
    user = await _user()
    ws = await _create_ws(user)  # no exception, so no half-created workspace
    await _social_login(social_app, monkeypatch, user.email, "gh-flaky")
    assert await _balance(ws) == 0
    assert await _grant_entries(str(user.id)) == []

    # The ledger recovers; the next sign-in lands the grant.
    monkeypatch.setattr(credits_service, "grant", real_grant)
    await _social_login(social_app, monkeypatch, user.email, "gh-flaky")
    assert await _balance(ws) == GRANT
    assert len(await _grant_entries(str(user.id))) == 1
