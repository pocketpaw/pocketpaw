"""A guest can sign up with Google/GitHub and KEEP the notebook they filled.

Created 2026-09-13. Reported from the live kiosk: "in guest mode the signup
modal does not show the sign in with google, github". The buttons were hidden
on purpose (paw-enterprise AuthDialog.svelte, 2026-09-01) because social
sign-in ran ``_resolve_user`` — sign in, or link by verified email, or CREATE A
NEW USER. A guest has no verified email and owns no identity, so it always fell
to create: a brand-new account, a brand-new workspace, and the guest's pages,
sessions and stored key stranded on an id nobody could reach again. Hiding the
buttons was the right call for the code as it stood, and it left the kiosk's
whole conversion path as email + password.

This file makes the guest case work so the buttons can come back. The vehicle
is the LINK flow, not the login flow: link already pins the acting user into
the state, re-checks it against the session cookie at the callback, and refuses
an identity another account owns. Upgrading a guest is that, plus flipping
``is_guest`` off — the same in-place promotion ``upgrade_guest`` performs for
email and password, which is what keeps the workspace attached.

The refusal tests are the ones that earn their space. Both name a case where
the guest's data CANNOT come along, and in each the guest row must be left
exactly as it was: a half-upgraded guest (email taken from a stranger, or
``is_guest`` already flipped before a refusal) is worse than a clean no.
"""

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")
os.environ.setdefault(
    "POCKETPAW_SOCIAL_REDIRECT_URI",
    "http://localhost:8888/api/v1/auth/social/callback",
)

import fakeredis.aioredis
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity
from pocketpaw_ee.cloud.models.user import User

_PASSWORD = "StrongPass123!"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def app(mongo_db, monkeypatch):  # noqa: ARG001
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420")

    # The mint route validates the key against the provider before creating
    # anything. That round trip is not what these tests are about.
    from pocketpaw_ee.cloud.byok import service as byok_service

    async def _ok(api_key, *, provider="anthropic", base_url=None, model=None):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(byok_service, "validate_key", _ok)

    # ``workspace_service.create`` (inside the mint) invalidates the realtime
    # AudienceResolver, which only exists after ``init_realtime``. Stubbed the
    # same way the workspace-service and guest tests do.
    from unittest.mock import MagicMock

    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: MagicMock())

    # The mint limiter is a process-global keyed on client IP, and every test
    # here arrives from the same one. Without a reset the second mint in the
    # file 429s and the failure reads as a bug in the flow under test.
    from pocketpaw_ee.cloud.auth.router import guest_mint_limiter

    monkeypatch.setattr(guest_mint_limiter, "allow", lambda _ip: True)

    return _build_app()


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _seed_user(email: str) -> User:
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=email, password=_PASSWORD))
        break
    return user


async def _mint_guest(client: AsyncClient) -> User:
    """Mint a guest and leave its session cookie on ``client``."""
    resp = await client.post("/api/v1/auth/guest", json={"api_key": "sk-ant-" + "x" * 40})
    assert resp.status_code in (200, 204), resp.text
    assert "paw_auth" in resp.cookies
    guest = await User.find_one(User.is_guest == True)  # noqa: E712
    assert guest is not None
    return guest


def _stub_exchange(monkeypatch, identity: SocialIdentity) -> None:
    from pocketpaw_ee.cloud.auth.social.providers.github import GitHubProvider

    async def fake_exchange(self, **kwargs):  # noqa: ANN001, ARG001
        return identity

    monkeypatch.setattr(GitHubProvider, "exchange", fake_exchange)


async def _link_through_callback(client: AsyncClient, monkeypatch, identity: SocialIdentity):
    resp = await client.post("/api/v1/auth/social/github/link")
    assert resp.status_code == 200, resp.text
    state = resp.json()["authorize_url"].split("state=")[1].split("&")[0]
    _stub_exchange(monkeypatch, identity)
    return await client.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------


async def test_a_guest_who_signs_up_with_github_keeps_the_same_account(app, monkeypatch):
    """The whole point: same user id, so the workspace and pages come along."""
    async with _client(app) as client:
        guest = await _mint_guest(client)
        guest_id = guest.id
        workspace_before = list(guest.workspaces or [])

        done = await _link_through_callback(
            client,
            monkeypatch,
            SocialIdentity(
                provider="github",
                account_id="gh-guest",
                email="newcomer@gmail.com",
            ),
        )
        assert done.status_code == 302, done.text

    after = await User.get(guest_id)
    assert after is not None, "the guest account was replaced, not upgraded"
    assert after.is_guest is False, "still a guest — the upgrade never happened"
    assert after.guest_limits is None, "guest caps survived the upgrade"
    assert after.email == "newcomer@gmail.com"
    assert [a.oauth_name for a in after.oauth_accounts] == ["github"]
    assert list(after.workspaces or []) == workspace_before, "lost the workspace"

    # And no second account was minted behind their back.
    assert await User.find(User.email == "newcomer@gmail.com").count() == 1


async def test_the_upgraded_guest_can_come_back_through_plain_social_login(app, monkeypatch):
    """An upgrade nobody can sign in to afterwards is not an upgrade.

    The identity must be attached well enough that the ordinary login flow's
    ``_find_by_oauth_account`` finds it and returns the SAME user.
    """
    identity = SocialIdentity(
        provider="github",
        account_id="gh-returning",
        email="returning@gmail.com",
    )
    async with _client(app) as client:
        guest = await _mint_guest(client)
        guest_id = guest.id
        await _link_through_callback(client, monkeypatch, identity)

    # The identity itself, not just the address. If only the email were written
    # the return trip below would still work — via ``_find_by_email`` — and
    # would quietly break when the user changes their address at GitHub.
    assert [a.account_id for a in (await User.get(guest_id)).oauth_accounts] == ["gh-returning"]

    async with _client(app) as fresh_browser:
        _stub_exchange(monkeypatch, identity)
        start = await fresh_browser.get(
            "/api/v1/auth/social/github/login", params={"flow": "web"}, follow_redirects=False
        )
        state = start.headers["location"].split("state=")[1].split("&")[0]
        back = await fresh_browser.get(
            "/api/v1/auth/social/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
        assert back.status_code == 302, back.text
        assert "paw_auth" in back.cookies

    assert await User.find(User.email == "returning@gmail.com").count() == 1
    assert (await User.get(guest_id)).email == "returning@gmail.com"


# ---------------------------------------------------------------------------
# The two refusals — a guest whose data cannot come along is left untouched
# ---------------------------------------------------------------------------


async def test_a_provider_email_owned_by_someone_else_refuses_and_changes_nothing(app, monkeypatch):
    """``upgrade_guest`` already refuses a taken email with auth.email_taken.

    The social path has to refuse it too, and for the same reason: two rows
    cannot hold one address, and merging the guest's pages into the stranger's
    account is not something this flow is allowed to decide.
    """
    await _seed_user("taken@gmail.com")

    async with _client(app) as client:
        guest = await _mint_guest(client)
        guest_id = guest.id

        done = await _link_through_callback(
            client,
            monkeypatch,
            SocialIdentity(
                provider="github",
                account_id="gh-collide",
                email="taken@gmail.com",
            ),
        )
        # A callback answers with a redirect carrying an error, never a 500.
        assert done.status_code == 302, done.text
        assert "error" in done.headers["location"]

    after = await User.get(guest_id)
    assert after.is_guest is True, "left half-upgraded after a refusal"
    assert after.guest_limits is not None
    assert after.email.endswith("@guest.invalid"), "took a stranger's address"
    assert after.oauth_accounts == []


async def test_an_identity_another_account_already_owns_refuses_and_changes_nothing(
    app, monkeypatch
):
    """Someone signs into the kiosk as a guest, then picks the GitHub account
    they already used to register. Their existing account owns that identity,
    so it cannot be re-pointed at the guest row.
    """
    owner = await _seed_user("owner@acme.com")
    identity = SocialIdentity(
        provider="github",
        account_id="gh-owned",
        email="owner-alt@gmail.com",
    )
    async with _client(app) as owner_browser:
        resp = await owner_browser.post(
            "/api/v1/auth/login",
            data={"username": "owner@acme.com", "password": _PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code in (200, 204)
        await _link_through_callback(owner_browser, monkeypatch, identity)
    assert [a.oauth_name for a in (await User.get(owner.id)).oauth_accounts] == ["github"]

    async with _client(app) as client:
        guest = await _mint_guest(client)
        guest_id = guest.id
        done = await _link_through_callback(client, monkeypatch, identity)
        assert done.status_code == 302
        assert "error" in done.headers["location"]

    after = await User.get(guest_id)
    assert after.is_guest is True
    assert after.oauth_accounts == []


# ---------------------------------------------------------------------------
# Regression guard on the path this change reuses
# ---------------------------------------------------------------------------


async def test_a_registered_user_linking_an_account_is_not_turned_into_anything(app, monkeypatch):
    """The guest flip must key on ``is_guest``, not on "a link just happened".

    Without this, connecting GitHub from Settings would rewrite a registered
    user's email to whatever the provider returned.
    """
    alice = await _seed_user("alice@acme.com")
    async with _client(app) as client:
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": "alice@acme.com", "password": _PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code in (200, 204)
        done = await _link_through_callback(
            client,
            monkeypatch,
            SocialIdentity(
                provider="github",
                account_id="gh-alice",
                email="alice@github.com",
            ),
        )
        assert done.status_code == 302
        assert "error" not in done.headers["location"], (
            "a plain Settings link was refused: " + done.headers["location"]
        )
        assert "social_linked=github" in done.headers["location"]

    after = await User.get(alice.id)
    assert after.email == "alice@acme.com", "a plain link rewrote the account email"
    assert after.is_guest is False
    assert [a.oauth_name for a in after.oauth_accounts] == ["github"]
