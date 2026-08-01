"""Social sign-in service + routes (AM-2/AM-3/AM-4) against the real app.

test_social_linking.py proves the policy in isolation. This file proves the
service actually APPLIES it — that the right lookups feed the table, that a
refusal never mints a session, and that a created account is only ever created
from a verified address.

The provider is stubbed at the adapter boundary (``exchange``), so these
exercise our orchestration rather than Google's or GitHub's HTTP.
"""

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")
os.environ.setdefault(
    "POCKETPAW_SOCIAL_REDIRECT_URI",
    "http://localhost:8888/api/v1/auth/social/callback",
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
from pocketpaw_ee.cloud.auth.social import domain
from pocketpaw_ee.cloud.auth.social import service as social_service
from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import SsoConfig, Workspace

_EXISTING_EMAIL = "existing@acme.com"
_EXISTING_PASSWORD = "StrongPass123!"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_user(email: str = _EXISTING_EMAIL) -> User:
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=email, password=_EXISTING_PASSWORD))
        break
    return user


@pytest_asyncio.fixture
async def env(mongo_db, monkeypatch):  # noqa: ARG001
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420")
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _stub_exchange(monkeypatch, identity: SocialIdentity):
    """Make the github adapter return ``identity`` without touching the network."""
    from pocketpaw_ee.cloud.auth.social.providers.github import GitHubProvider

    async def fake_exchange(self, **kwargs):  # noqa: ANN001, ARG001
        return identity

    monkeypatch.setattr(GitHubProvider, "exchange", fake_exchange)


async def _begin(client) -> str:
    """Start a real flow and return the state the provider would echo back."""
    resp = await client.get(
        "/api/v1/auth/social/github/login", params={"flow": "web"}, follow_redirects=False
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    return location.split("state=")[1].split("&")[0]


# ---------------------------------------------------------------------------
# Provider listing
# ---------------------------------------------------------------------------


async def test_configured_provider_is_listed(env):
    resp = await env.get("/api/v1/auth/social/providers")
    assert resp.status_code == 200
    assert "github" in resp.json()["providers"]


async def test_unconfigured_provider_login_is_a_clean_redirect_not_a_500(env, monkeypatch):
    monkeypatch.delenv("POCKETPAW_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("POCKETPAW_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    resp = await env.get("/api/v1/auth/social/google/login", follow_redirects=False)
    assert resp.status_code == 302
    assert "social.provider_not_configured" in resp.headers["location"]


async def test_unknown_provider_is_refused(env):
    resp = await env.get("/api/v1/auth/social/myspace/login", follow_redirects=False)
    assert resp.status_code == 302
    assert "social.unknown_provider" in resp.headers["location"]


# ---------------------------------------------------------------------------
# begin_login -> state
# ---------------------------------------------------------------------------


async def test_login_redirects_to_the_provider_with_a_state(env):
    resp = await env.get("/api/v1/auth/social/github/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://github.com/login/oauth/authorize")
    assert "state=" in resp.headers["location"]


async def test_the_state_is_single_use_across_the_callback(env, monkeypatch):
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-1", email="new@acme.com"),
    )
    state = await _begin(env)

    first = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert first.status_code == 302
    assert "auth_error" not in first.headers["location"]

    # Replaying the same state must not produce a second session.
    second = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert "social.invalid_state" in second.headers["location"]


async def test_a_forged_state_is_refused(env):
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": "made-up"},
        follow_redirects=False,
    )
    assert "social.invalid_state" in resp.headers["location"]


async def test_missing_code_or_state_is_refused(env):
    resp = await env.get("/api/v1/auth/social/callback", follow_redirects=False)
    assert "social.missing_code_or_state" in resp.headers["location"]


async def test_provider_side_error_is_passed_through(env):
    # The user pressed Cancel on the consent screen.
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert "access_denied" in resp.headers["location"]


# ---------------------------------------------------------------------------
# The policy, applied
# ---------------------------------------------------------------------------


async def test_verified_email_with_no_account_creates_one_and_sets_a_cookie(env, monkeypatch):
    _stub_exchange(
        monkeypatch,
        SocialIdentity(
            provider="github", account_id="gh-new", email="fresh@acme.com", full_name="Fresh"
        ),
    )
    state = await _begin(env)
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert "set-cookie" in {k.lower() for k in resp.headers.keys()}

    created = await User.find_one(User.email == "fresh@acme.com")
    assert created is not None
    # Honest only because the provider asserted verification.
    assert created.is_verified is True
    assert created.oauth_accounts[0].oauth_name == "github"
    assert created.oauth_accounts[0].account_id == "gh-new"


async def test_created_accounts_never_store_a_provider_access_token(env, monkeypatch):
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-tok", email="tok@acme.com"),
    )
    state = await _begin(env)
    await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    created = await User.find_one(User.email == "tok@acme.com")
    # Sign-in needs identity, not ongoing API access. A stored token we never
    # use is avoidable breach surface.
    assert created.oauth_accounts[0].access_token == ""


async def test_verified_email_links_to_the_existing_account(env, monkeypatch):
    existing = await _seed_user()
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-link", email=_EXISTING_EMAIL),
    )
    state = await _begin(env)
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert "auth_error" not in resp.headers["location"]

    refreshed = await User.get(existing.id)
    assert [a.account_id for a in refreshed.oauth_accounts] == ["gh-link"]
    # No duplicate account was minted for the same address.
    assert await User.find(User.email == _EXISTING_EMAIL).count() == 1


async def test_UNVERIFIED_email_matching_an_account_is_REFUSED(env, monkeypatch):
    # The takeover attempt, end to end: attacker's provider account carries the
    # victim's address, unverified. No session may be minted.
    existing = await _seed_user()
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-attacker", email=None),
    )
    state = await _begin(env)
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    assert domain.REFUSE_UNVERIFIED in resp.headers["location"]
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}

    refreshed = await User.get(existing.id)
    assert refreshed.oauth_accounts == []


async def test_a_returning_linked_account_signs_in_without_a_verified_email(env, monkeypatch):
    existing = await _seed_user("returning@acme.com")
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-ret", email="returning@acme.com"),
    )
    state = await _begin(env)
    await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    # Second visit: the provider no longer vouches for any address (scope
    # declined on re-consent). They are still the same person.
    _stub_exchange(
        monkeypatch, SocialIdentity(provider="github", account_id="gh-ret", email=None)
    )
    state2 = await _begin(env)
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state2},
        follow_redirects=False,
    )

    assert "auth_error" not in resp.headers["location"]
    assert await User.find(User.email == "returning@acme.com").count() == 1
    assert str((await User.get(existing.id)).id) == str(existing.id)


async def test_linking_twice_does_not_duplicate_the_oauth_account(env, monkeypatch):
    existing = await _seed_user("dupe@acme.com")
    ident = SocialIdentity(provider="github", account_id="gh-dupe", email="dupe@acme.com")
    for _ in range(2):
        _stub_exchange(monkeypatch, ident)
        state = await _begin(env)
        await env.get(
            "/api/v1/auth/social/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
    refreshed = await User.get(existing.id)
    assert len(refreshed.oauth_accounts) == 1


# ---------------------------------------------------------------------------
# Enforced SSO
# ---------------------------------------------------------------------------


async def test_enforced_sso_refuses_social_sign_in(env, monkeypatch):
    user = await _seed_user("member@sso.com")
    ws = Workspace(
        name="SSO Corp",
        slug="ssocorp",
        owner=str(user.id),
        sso_config=SsoConfig(
            provider="okta",
            issuer="https://idp.example.com",
            client_id="x",
            client_secret_encrypted="ciphertext",
            allowed_domains=["sso.com"],
            enforced=True,
        ),
    )
    await ws.insert()
    user.workspaces.append(WorkspaceMembership(workspace=str(ws.id), role="member"))
    await user.save()

    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-sso", email="member@sso.com"),
    )
    state = await _begin(env)
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    # Social must not be the documented way around the control we sell.
    assert domain.REFUSE_SSO_ENFORCED in resp.headers["location"]
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


async def test_sso_enforcement_check_fails_CLOSED(mongo_db, monkeypatch):  # noqa: ARG001
    user = await _seed_user("closed@acme.com")
    # A well-formed id, so the code reaches the query the stub blows up.
    user.workspaces.append(
        WorkspaceMembership(workspace=str(PydanticObjectId()), role="member")
    )

    def boom(*a, **k):  # noqa: ANN001, ARG001
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(Workspace, "find", boom)
    # A lookup failure must not be read as "no enforcement".
    assert await social_service._sso_enforced_for(user) is True


# ---------------------------------------------------------------------------
# next=
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", ["//evil.com", "https://evil.com", "/\\evil.com"])
async def test_hostile_next_is_not_honoured(env, monkeypatch, hostile):
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-next", email="next@acme.com"),
    )
    resp = await env.get(
        "/api/v1/auth/social/github/login",
        params={"flow": "web", "next": hostile},
        follow_redirects=False,
    )
    state = resp.headers["location"].split("state=")[1].split("&")[0]

    done = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    # The endpoint is reachable directly, so it re-checks rather than trusting
    # the frontend's parse-time validation.
    assert done.headers["location"] == "http://localhost:1420/"


async def test_a_safe_next_is_honoured(env, monkeypatch):
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-ok", email="ok@acme.com"),
    )
    resp = await env.get(
        "/api/v1/auth/social/github/login",
        params={"flow": "web", "next": "/calendar"},
        follow_redirects=False,
    )
    state = resp.headers["location"].split("state=")[1].split("&")[0]

    done = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert done.headers["location"] == "http://localhost:1420/calendar"


async def test_refusal_raises_forbidden_from_the_service_layer(mongo_db, monkeypatch):  # noqa: ARG001
    # Belt and braces: the router turns this into a redirect, but the service
    # must not return a user on a refusal under any circumstance.
    with pytest.raises(Forbidden):
        await social_service._resolve_user(
            SocialIdentity(provider="github", account_id="nope", email=None)
        )


async def test_unparseable_workspace_id_is_skipped_not_crashed(mongo_db):  # noqa: ARG001
    # A malformed id cannot name a real workspace, so there is no enforcement
    # to miss - but it must not raise on the way past.
    user = await _seed_user("junkws@acme.com")
    user.workspaces.append(WorkspaceMembership(workspace="not-an-objectid", role="member"))
    assert await social_service._sso_enforced_for(user) is False


async def test_membership_ids_are_strings_and_must_be_cast(mongo_db):  # noqa: ARG001
    # Pins the shape mismatch behind the enforcement bug: memberships store the
    # workspace id as a STRING while _id is an ObjectId, so a raw $in matches
    # nothing and would silently disable the guard.
    user = await _seed_user("shape@acme.com")
    ws = Workspace(name="Shape", slug="shape", owner=str(user.id))
    await ws.insert()
    user.workspaces.append(WorkspaceMembership(workspace=str(ws.id), role="member"))
    assert isinstance(user.workspaces[-1].workspace, str)
    # No sso_config -> not enforced, but the lookup must have actually MATCHED.
    found = [w async for w in Workspace.find({"_id": {"$in": [PydanticObjectId(ws.id)]}})]
    assert len(found) == 1
    assert [w async for w in Workspace.find({"_id": {"$in": [str(ws.id)]}})] == []


# ---------------------------------------------------------------------------
# Desktop flow — the one-time exchange code (AM-5)
# ---------------------------------------------------------------------------


async def test_desktop_flow_redirects_with_a_code_and_NO_token(env, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420")
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-desk", email="desk@acme.com"),
    )
    resp = await env.get(
        "/api/v1/auth/social/github/login",
        params={"flow": "desktop"},
        follow_redirects=False,
    )
    state = resp.headers["location"].split("state=")[1].split("&")[0]

    done = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    location = done.headers["location"]

    # Goes to the FRONTEND origin, where /oauth-callback actually lives.
    assert location.startswith("http://localhost:1420/oauth-callback?xc=")
    # The whole point: a reference, never a credential.
    assert "access_token" not in location
    assert "token=" not in location
    # And no cookie either - desktop uses bearer.
    assert "set-cookie" not in {k.lower() for k in done.headers.keys()}


async def _desktop_code(client, monkeypatch, email="desk2@acme.com", account="gh-d2") -> str:
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420")
    _stub_exchange(
        monkeypatch, SocialIdentity(provider="github", account_id=account, email=email)
    )
    resp = await client.get(
        "/api/v1/auth/social/github/login",
        params={"flow": "desktop"},
        follow_redirects=False,
    )
    state = resp.headers["location"].split("state=")[1].split("&")[0]
    done = await client.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    return done.headers["location"].split("xc=")[1].split("&")[0]


async def test_exchange_returns_a_bearer_token(env, monkeypatch):
    xc = await _desktop_code(env, monkeypatch)
    resp = await env.post("/api/v1/auth/social/exchange", json={"xc": xc})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"].lower() == "bearer"
    assert body["access_token"]


async def test_an_exchange_code_is_single_use(env, monkeypatch):
    xc = await _desktop_code(env, monkeypatch)
    first = await env.post("/api/v1/auth/social/exchange", json={"xc": xc})
    assert first.status_code == 200

    second = await env.post("/api/v1/auth/social/exchange", json={"xc": xc})
    assert second.status_code == 403
    assert "invalid" in second.text.lower()


async def test_a_forged_exchange_code_is_refused(env):
    resp = await env.post("/api/v1/auth/social/exchange", json={"xc": "made-up-code"})
    assert resp.status_code == 403


async def test_an_exchange_code_cannot_be_spent_as_a_login_state(env, monkeypatch):
    # Different namespace: the two stores must not be interchangeable.
    xc = await _desktop_code(env, monkeypatch)
    resp = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": xc},
        follow_redirects=False,
    )
    assert "social.invalid_state" in resp.headers["location"]


async def test_a_login_state_cannot_be_spent_as_an_exchange_code(env, monkeypatch):
    _stub_exchange(
        monkeypatch, SocialIdentity(provider="github", account_id="gh-x", email="x@acme.com")
    )
    state = await _begin(env)
    resp = await env.post("/api/v1/auth/social/exchange", json={"xc": state})
    assert resp.status_code == 403


async def test_a_deactivated_account_cannot_redeem_its_code(env, monkeypatch):
    # The window is 60s, but an admin may disable the account inside it.
    xc = await _desktop_code(env, monkeypatch, email="gone@acme.com", account="gh-gone")
    user = await User.find_one(User.email == "gone@acme.com")
    user.is_active = False
    await user.save()

    resp = await env.post("/api/v1/auth/social/exchange", json={"xc": xc})
    assert resp.status_code == 403


async def test_exchange_rejects_an_empty_code(env):
    resp = await env.post("/api/v1/auth/social/exchange", json={"xc": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Redirect targets (found on the first live Google run)
# ---------------------------------------------------------------------------


async def test_success_redirects_to_the_FRONTEND_not_the_api(env, monkeypatch):
    # The bug: a relative redirect resolves against the API origin. Locally the
    # SPA is :1420 and the API is :8888, so a signed-in user landed on the API
    # root and saw nothing.
    _stub_exchange(
        monkeypatch,
        SocialIdentity(provider="github", account_id="gh-origin", email="origin@acme.com"),
    )
    state = await _begin(env)
    done = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    location = done.headers["location"]
    assert location.startswith("http://localhost:1420"), location
    # Absolute, so the browser cannot resolve it against the API origin.
    assert not location.startswith("/")


async def test_a_refusal_reopens_the_dialog_instead_of_a_dead_route(env, monkeypatch):
    # /auth/error does not exist in the SPA - only forgot, reset, verify - so
    # the old target was a 404 on the wrong origin. A refusal is a UI state.
    _stub_exchange(
        monkeypatch, SocialIdentity(provider="github", account_id="gh-nope", email=None)
    )
    state = await _begin(env)
    done = await env.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    location = done.headers["location"]

    assert location.startswith("http://localhost:1420/?auth=signin"), location
    assert "auth_error=auth.unverified_link" in location
    assert "/auth/error" not in location


async def test_every_error_path_targets_the_frontend(env):
    for params in (
        {"error": "access_denied"},
        {},  # missing code/state
        {"code": "c", "state": "forged"},
    ):
        resp = await env.get(
            "/api/v1/auth/social/callback", params=params, follow_redirects=False
        )
        assert resp.headers["location"].startswith("http://localhost:1420/?auth=signin"), params
