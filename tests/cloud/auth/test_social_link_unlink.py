"""Connected accounts (AM-6) — list / link / unlink, against the real app.

test_social_linking.py proves the policy in isolation. This file proves the
SERVICE and the ROUTES apply it, and it is the layer that matters most here:
the enforced-SSO bug earlier on this branch passed every unit test in that file
and was caught only by an end-to-end one, because the defect was in how the
service fed the table, not in the table.

Four properties are load-bearing, and each has a test that fails loudly if it
regresses:

  * the three routes REFUSE an anonymous caller (a link endpoint that trusts
    anything but the session is an account-takeover primitive);
  * completing a link state from a different session attaches NOTHING;
  * an identity already owned by another account cannot be re-pointed;
  * a user cannot unlink their way out of their own account — including the
    social-only user whose stored password hash is an unusable sentinel, which
    is the case a naive ``bool(hashed_password)`` check gets wrong.

The provider is stubbed at the adapter boundary (``exchange``), so these
exercise our orchestration rather than GitHub's HTTP.
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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.auth.social import domain
from pocketpaw_ee.cloud.auth.social import service as social_service
from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity
from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import SsoConfig, Workspace

_PASSWORD = "StrongPass123!"


def _build_app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


async def _seed_user(email: str) -> User:
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=email, password=_PASSWORD))
        break
    return user


@pytest_asyncio.fixture
async def app(mongo_db, monkeypatch):  # noqa: ARG001
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "http://localhost:1420")
    return _build_app()


def _client(app: FastAPI) -> AsyncClient:
    """A fresh client — i.e. a fresh browser, with its own cookie jar."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest_asyncio.fixture
async def anon(app):
    async with _client(app) as client:
        yield client


async def _sign_in(client: AsyncClient, email: str) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code in (200, 204), resp.text
    assert "paw_auth" in resp.cookies


def _stub_exchange(monkeypatch, identity: SocialIdentity) -> None:
    from pocketpaw_ee.cloud.auth.social.providers.github import GitHubProvider

    async def fake_exchange(self, **kwargs):  # noqa: ANN001, ARG001
        return identity

    monkeypatch.setattr(GitHubProvider, "exchange", fake_exchange)


async def _start_link(client: AsyncClient, *, next_path: str | None = None) -> str:
    """Begin a link as the client's signed-in user; return the state value."""
    params = {"next": next_path} if next_path else None
    resp = await client.post("/api/v1/auth/social/github/link", params=params)
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    return url.split("state=")[1].split("&")[0]


async def _callback(client: AsyncClient, state: str):
    return await client.get(
        "/api/v1/auth/social/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )


async def _social_sign_in(client: AsyncClient, monkeypatch, identity: SocialIdentity):
    """Create-or-sign-in through the social flow; leaves a cookie on ``client``."""
    _stub_exchange(monkeypatch, identity)
    resp = await client.get(
        "/api/v1/auth/social/github/login", params={"flow": "web"}, follow_redirects=False
    )
    state = resp.headers["location"].split("state=")[1].split("&")[0]
    return await _callback(client, state)


# ---------------------------------------------------------------------------
# The routes refuse an anonymous caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/auth/social/identities"),
        ("POST", "/api/v1/auth/social/github/link"),
        ("DELETE", "/api/v1/auth/social/identities/github"),
    ],
)
async def test_the_connected_account_routes_require_a_session(anon, method, path):
    # The whole point of the feature. Any of these acting on a caller-supplied
    # identity instead of a session would be an account-takeover primitive.
    resp = await anon.request(method, path)
    assert resp.status_code == 401, resp.text


async def test_link_does_not_take_its_target_user_from_the_request(app, monkeypatch):
    # There is no request shape that names a victim: the provider is the only
    # caller-chosen value, and the account comes from the cookie. Asserted by
    # showing a forged tenancy header changes nothing about who gets linked.
    victim = await _seed_user("victim@acme.com")
    await _seed_user("attacker@acme.com")
    async with _client(app) as attacker:
        await _sign_in(attacker, "attacker@acme.com")
        resp = await attacker.post(
            "/api/v1/auth/social/github/link",
            headers={"X-PocketPaw-User-Id": str(victim.id)},
        )
        assert resp.status_code == 200
        state = resp.json()["authorize_url"].split("state=")[1].split("&")[0]

        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-f", email="a@acme.com"),
        )
        await _callback(attacker, state)

    assert (await User.get(victim.id)).oauth_accounts == []


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_signed_in_user_links_lists_and_unlinks(app, monkeypatch):
    await _seed_user("alice@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "alice@acme.com")

        empty = await client.get("/api/v1/auth/social/identities")
        assert empty.status_code == 200
        assert empty.json()["identities"] == []

        state = await _start_link(client, next_path="/settings")
        _stub_exchange(
            monkeypatch,
            SocialIdentity(
                provider="github", account_id="gh-alice", email="alice@github.com"
            ),
        )
        done = await _callback(client, state)

        assert done.status_code == 302
        location = done.headers["location"]
        # Back to the page that started it, on the FRONTEND origin, carrying
        # the outcome — not to the sign-in dialog.
        assert location.startswith("http://localhost:1420/settings?"), location
        assert "social_linked=github" in location
        assert "auth=signin" not in location

        listed = await client.get("/api/v1/auth/social/identities")
        rows = listed.json()["identities"]
        assert len(rows) == 1
        assert rows[0]["provider"] == "github"
        assert rows[0]["account_email"] == "alice@github.com"
        assert rows[0]["linked_at"] is not None

        gone = await client.delete("/api/v1/auth/social/identities/github")
        assert gone.status_code == 204
        assert (await client.get("/api/v1/auth/social/identities")).json()["identities"] == []


async def test_the_identity_list_never_leaks_tokens_or_provider_ids(app, monkeypatch):
    await _seed_user("quiet@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "quiet@acme.com")
        state = await _start_link(client)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-secret", email="q@acme.com"),
        )
        await _callback(client, state)

        row = (await client.get("/api/v1/auth/social/identities")).json()["identities"][0]

    # An endpoint reading a credential store hands back the minimum that
    # answers the question. The provider's account id is not part of that.
    assert set(row) == {"provider", "account_email", "linked_at"}
    assert "gh-secret" not in str(row)


async def test_linking_the_same_identity_twice_is_idempotent(app, monkeypatch):
    user = await _seed_user("twice@acme.com")
    identity = SocialIdentity(provider="github", account_id="gh-2x", email="t@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "twice@acme.com")
        for _ in range(2):
            state = await _start_link(client)
            _stub_exchange(monkeypatch, identity)
            done = await _callback(client, state)
            assert "social_error" not in done.headers["location"]

    assert len((await User.get(user.id)).oauth_accounts) == 1


# ---------------------------------------------------------------------------
# Linking must not steal
# ---------------------------------------------------------------------------


async def test_linking_an_identity_owned_by_another_user_is_REFUSED(app, monkeypatch):
    # The takeover attempt: attacker connects the victim's already-linked
    # GitHub to their own account, hoping we re-point it.
    identity = SocialIdentity(provider="github", account_id="gh-owned", email="v@acme.com")
    victim = await _seed_user("owner@acme.com")
    async with _client(app) as victim_client:
        await _sign_in(victim_client, "owner@acme.com")
        state = await _start_link(victim_client)
        _stub_exchange(monkeypatch, identity)
        await _callback(victim_client, state)

    attacker = await _seed_user("thief@acme.com")
    async with _client(app) as thief_client:
        await _sign_in(thief_client, "thief@acme.com")
        state = await _start_link(thief_client)
        _stub_exchange(monkeypatch, identity)
        done = await _callback(thief_client, state)

    assert f"social_error={domain.REFUSE_IDENTITY_CLAIMED}" in done.headers["location"]
    # And nothing moved: the victim keeps it, the attacker gains nothing.
    assert [a.account_id for a in (await User.get(victim.id)).oauth_accounts] == ["gh-owned"]
    assert (await User.get(attacker.id)).oauth_accounts == []


async def test_a_link_state_completed_by_a_DIFFERENT_session_attaches_nothing(
    app, monkeypatch
):
    # State is a bearer secret: whoever holds it wins. That is survivable for
    # sign-in, but for linking it would let a stolen state attach the thief's
    # identity to the victim's account. The session re-check is what stops it.
    victim = await _seed_user("target@acme.com")
    await _seed_user("other@acme.com")

    async with _client(app) as victim_client:
        await _sign_in(victim_client, "target@acme.com")
        stolen_state = await _start_link(victim_client)

    async with _client(app) as other_client:
        await _sign_in(other_client, "other@acme.com")
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-thief", email="o@acme.com"),
        )
        done = await _callback(other_client, stolen_state)

    assert f"social_error={domain.REFUSE_LINK_SESSION_MISMATCH}" in done.headers["location"]
    assert (await User.get(victim.id)).oauth_accounts == []
    assert (await User.find_one(User.email == "other@acme.com")).oauth_accounts == []


async def test_a_link_state_completed_with_NO_session_attaches_nothing(app, monkeypatch):
    victim = await _seed_user("nosession@acme.com")
    async with _client(app) as victim_client:
        await _sign_in(victim_client, "nosession@acme.com")
        stolen_state = await _start_link(victim_client)

    async with _client(app) as anonymous:
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-anon", email="n@acme.com"),
        )
        done = await _callback(anonymous, stolen_state)

    assert domain.REFUSE_LINK_SESSION_MISMATCH in done.headers["location"]
    assert (await User.get(victim.id)).oauth_accounts == []
    # And it did NOT fall through to the sign-in branch and mint a session.
    assert "set-cookie" not in {k.lower() for k in done.headers.keys()}


async def test_a_refusal_lands_back_on_the_page_that_started_the_link(app, monkeypatch):
    # The state is consumed before the refusal is raised, so ``next`` has to
    # travel on the exception. Without it the user is dropped on the app root
    # and the Settings panel never gets to explain what happened.
    await _seed_user("backhome@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "backhome@acme.com")
        state = await _start_link(client, next_path="/settings/accounts")
        _stub_exchange(
            monkeypatch, SocialIdentity(provider="github", account_id="gh-bh", email=None)
        )
        done = await _callback(client, state)

    location = done.headers["location"]
    assert location.startswith("http://localhost:1420/settings/accounts?"), location
    assert f"social_error={domain.REFUSE_UNVERIFIED}" in location


async def test_a_hostile_next_is_not_honoured_on_a_link_REFUSAL_either(app, monkeypatch):
    # The refusal path builds its own redirect, so it needs the same validator
    # as the success path rather than inheriting it by accident.
    await _seed_user("hostilefail@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "hostilefail@acme.com")
        state = await _start_link(client, next_path="https://evil.com")
        _stub_exchange(
            monkeypatch, SocialIdentity(provider="github", account_id="gh-hf", email=None)
        )
        done = await _callback(client, state)

    assert done.headers["location"].startswith("http://localhost:1420/?")
    assert "evil.com" not in done.headers["location"]


async def test_an_unverified_identity_cannot_be_linked(app, monkeypatch):
    user = await _seed_user("unver@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "unver@acme.com")
        state = await _start_link(client)
        _stub_exchange(
            monkeypatch, SocialIdentity(provider="github", account_id="gh-un", email=None)
        )
        done = await _callback(client, state)

    assert f"social_error={domain.REFUSE_UNVERIFIED}" in done.headers["location"]
    assert (await User.get(user.id)).oauth_accounts == []


# ---------------------------------------------------------------------------
# Enforced SSO
# ---------------------------------------------------------------------------


async def _enforce_sso(user: User) -> None:
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


async def test_enforced_sso_refuses_the_link_before_any_consent(app):
    # Refused at the START, so the user gets an explainable error in Settings
    # instead of a round trip through Google that ends in a redirect.
    user = await _seed_user("member@sso.com")
    await _enforce_sso(user)
    async with _client(app) as client:
        await _sign_in(client, "member@sso.com")
        resp = await client.post("/api/v1/auth/social/github/link")

    assert resp.status_code == 403
    assert domain.REFUSE_SSO_ENFORCED in resp.text


async def test_enforced_sso_refuses_the_link_at_the_CALLBACK_too(app, monkeypatch):
    # Defence in depth: enforcement can be switched on between authorize and
    # callback, and the begin-time check is a UX affordance, not the control.
    user = await _seed_user("late@sso.com")
    async with _client(app) as client:
        await _sign_in(client, "late@sso.com")
        state = await _start_link(client)

        await _enforce_sso(user)

        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-late", email="l@sso.com"),
        )
        done = await _callback(client, state)

    assert f"social_error={domain.REFUSE_SSO_ENFORCED}" in done.headers["location"]
    assert (await User.get(user.id)).oauth_accounts == []


# ---------------------------------------------------------------------------
# Never remove the last credential
# ---------------------------------------------------------------------------


async def test_a_social_only_user_cannot_unlink_their_only_identity(app, monkeypatch):
    # THE lockout case, and the one a naive bool(hashed_password) check gets
    # wrong: this account has a NON-EMPTY hashed_password, because the social
    # create path stores an unusable "!social-only-" sentinel there rather than
    # an empty string. Read as "has a password", this unlink would succeed and
    # leave an account nobody can ever sign into again.
    async with _client(app) as client:
        await _social_sign_in(
            client,
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-only", email="only@acme.com"),
        )
        resp = await client.delete("/api/v1/auth/social/identities/github")

    assert resp.status_code == 409
    assert domain.REFUSE_LAST_CREDENTIAL in resp.text

    user = await User.find_one(User.email == "only@acme.com")
    assert [a.oauth_name for a in user.oauth_accounts] == ["github"]
    # The trap, stated directly so the next reader sees why the check is not
    # a truthiness test.
    assert user.hashed_password
    assert social_service._has_usable_password(user) is False


async def test_a_password_user_may_unlink_their_only_identity(app, monkeypatch):
    user = await _seed_user("haspw@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "haspw@acme.com")
        state = await _start_link(client)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-pw", email="h@acme.com"),
        )
        await _callback(client, state)

        resp = await client.delete("/api/v1/auth/social/identities/github")

    assert resp.status_code == 204
    assert (await User.get(user.id)).oauth_accounts == []


async def test_a_social_only_user_with_two_identities_may_unlink_one(app, monkeypatch):
    async with _client(app) as client:
        await _social_sign_in(
            client,
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-a", email="two@acme.com"),
        )
        user = await User.find_one(User.email == "two@acme.com")
        # A second identity, as if they had also connected Google.
        user.oauth_accounts.append(
            type(user.oauth_accounts[0])(
                oauth_name="google",
                account_id="goog-a",
                account_email="two@acme.com",
                access_token="",  # noqa: S106 — mirrors what the service stores
            )
        )
        await user.save()

        resp = await client.delete("/api/v1/auth/social/identities/github")

    assert resp.status_code == 204
    refreshed = await User.get(user.id)
    assert [a.oauth_name for a in refreshed.oauth_accounts] == ["google"]


async def test_unlinking_a_provider_that_was_never_linked_is_a_404(app):
    await _seed_user("none@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "none@acme.com")
        resp = await client.delete("/api/v1/auth/social/identities/google")

    assert resp.status_code == 404
    # The DOMAIN's code, not one derived from a human-readable resource name.
    # Raising NotFound("social identity", …) built the code as
    # f"{resource}.not_found" and put a SPACE in a machine-readable identifier,
    # which clients cannot key on — the frontend had grown a second lookup
    # entry for the mangled spelling before this was fixed.
    assert domain.REFUSE_NOT_LINKED in resp.text
    assert "social identity" not in resp.text


async def test_every_unlink_refusal_code_is_a_dotted_identifier(app, monkeypatch):
    # One assertion over both refusals this route can produce: an error code is
    # read by machines, so neither may carry a space. That is exactly what the
    # not_linked case did before it stopped borrowing NotFound's derived code.
    await _seed_user("codes@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "codes@acme.com")
        not_linked = await client.delete("/api/v1/auth/social/identities/google")

    # A social-only account, so its single identity is the last credential.
    # Needs its own client: it signs in through the social flow, not a password.
    async with _client(app) as social_only:
        await _social_sign_in(
            social_only,
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-c", email="lastcred@acme.com"),
        )
        last_credential = await social_only.delete("/api/v1/auth/social/identities/github")

    assert not_linked.status_code == 404
    assert last_credential.status_code == 409
    for resp in (not_linked, last_credential):
        code = resp.json()["error"]["code"]
        assert " " not in code, code
        assert code.startswith("auth."), code


async def test_unlink_only_ever_removes_the_named_provider(app, monkeypatch):
    user = await _seed_user("multi@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "multi@acme.com")
        state = await _start_link(client)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-m", email="m@acme.com"),
        )
        await _callback(client, state)

        resp = await client.delete("/api/v1/auth/social/identities/google")
        assert resp.status_code == 404

    # A 404 on one provider must not have disturbed the other.
    assert [a.oauth_name for a in (await User.get(user.id)).oauth_accounts] == ["github"]


# ---------------------------------------------------------------------------
# The link flow does not disturb the sign-in flow
# ---------------------------------------------------------------------------


async def test_a_link_callback_never_mints_a_session(app, monkeypatch):
    # The user already has one. Minting a second on this path would rotate
    # their session as a side effect of visiting Settings.
    await _seed_user("nomint@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "nomint@acme.com")
        state = await _start_link(client)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-nm", email="nm@acme.com"),
        )
        done = await _callback(client, state)

    assert "set-cookie" not in {k.lower() for k in done.headers.keys()}


async def test_a_plain_sign_in_still_works_while_signed_in_as_someone_else(
    app, monkeypatch
):
    # The sign-in branch must not be captured by the link branch just because
    # a session happens to be present — they are told apart by the state
    # payload, not by whether a cookie arrived.
    await _seed_user("already@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "already@acme.com")
        done = await _social_sign_in(
            client,
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-sw", email="sw@acme.com"),
        )

    assert done.status_code == 302
    assert "social_error" not in done.headers["location"]
    # A sign-in DOES mint a session, unlike the link branch above.
    assert "set-cookie" in {k.lower() for k in done.headers.keys()}
    assert await User.find_one(User.email == "sw@acme.com") is not None


async def test_a_link_state_cannot_be_spent_as_an_exchange_code(app):
    # Namespaces already separate these, but the link payload now carries a
    # user id — exactly the shape the exchange redeemer reads.
    await _seed_user("ns@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "ns@acme.com")
        state = await _start_link(client)
        resp = await client.post("/api/v1/auth/social/exchange", json={"xc": state})

    assert resp.status_code == 403


async def test_a_link_cannot_be_started_for_an_unconfigured_provider(app, monkeypatch):
    # Cleared explicitly: a developer .env may well have real Google
    # credentials, and the assertion is about the unconfigured branch.
    monkeypatch.delenv("POCKETPAW_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("POCKETPAW_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    await _seed_user("unconf@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "unconf@acme.com")
        resp = await client.post("/api/v1/auth/social/google/link")

    assert resp.status_code == 503
    assert "social.provider_not_configured" in resp.text


async def test_a_link_cannot_be_started_for_an_unknown_provider(app):
    await _seed_user("myspace@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "myspace@acme.com")
        resp = await client.post("/api/v1/auth/social/myspace/link")

    assert resp.status_code == 422
    assert "social.unknown_provider" in resp.text


async def test_a_hostile_next_on_a_link_does_not_leave_the_origin(app, monkeypatch):
    await _seed_user("hostile@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "hostile@acme.com")
        state = await _start_link(client, next_path="//evil.com")
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-h", email="h2@acme.com"),
        )
        done = await _callback(client, state)

    assert done.headers["location"].startswith("http://localhost:1420/?"), done.headers[
        "location"
    ]
    assert "evil.com" not in done.headers["location"]


# ---------------------------------------------------------------------------
# Desktop linking (AM-6 desktop)
# ---------------------------------------------------------------------------
#
# The desktop client is NOT "the web client in a window". It authenticates with
# a bearer from localStorage and holds no cookie for this origin — verified:
# the desktop login path returns a token and sets no Set-Cookie at all. So the
# Tauri webview that finishes consent cannot prove who it is, the callback
# cannot attach anything, and the identity is parked for the app to redeem
# under its bearer.
#
# These tests drive a real bearer rather than the cookie jar for exactly that
# reason. Driving them with a cookie would pass while proving nothing about
# the client this code exists for.


async def _bearer(client: AsyncClient, email: str) -> dict[str, str]:
    """A real bearer token, the way the desktop client authenticates."""
    resp = await client.post(
        "/api/v1/auth/bearer/login",
        data={"username": email, "password": _PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _start_desktop_link(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/auth/social/github/link", params={"flow": "desktop"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["authorize_url"].split("state=")[1].split("&")[0]


def _link_code(response) -> str:
    return response.headers["location"].split("link=")[1].split("&")[0]


async def test_begin_link_refuses_an_unknown_flow(app):
    # Refused rather than defaulted to web. A silent fallback would send a
    # desktop client down the branch whose cookie check it can never satisfy,
    # and the symptom is a webview that consents and then does nothing.
    await _seed_user("badflow@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "badflow@acme.com")
        resp = await client.post(
            "/api/v1/auth/social/github/link", params={"flow": "carrier-pigeon"}
        )

    assert resp.status_code == 422
    assert "social.unknown_flow" in resp.text


async def test_a_desktop_link_callback_parks_and_attaches_NOTHING(app, monkeypatch):
    # The bug this path fixes: the webview has no cookie, so the callback
    # cannot authenticate anyone. It must not attach on the state alone.
    user = await _seed_user("desk@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "desk@acme.com")
        state = await _start_desktop_link(client, headers)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-dk", email="dk@acme.com"),
        )
        # No cookie on this request — exactly what a Tauri webview sends.
        done = await _callback(client, state)

    location = done.headers["location"]
    assert location.startswith("http://localhost:1420/oauth-callback?"), location
    assert "link=" in location
    assert "provider=github" in location
    # `xc=` is the LOGIN branch's marker. Using it here would make the webview
    # try to trade this for a bearer, which is not what it is.
    assert "xc=" not in location
    assert "set-cookie" not in {k.lower() for k in done.headers.keys()}
    # Nothing attached yet. That is the point.
    assert (await User.get(user.id)).oauth_accounts == []


async def test_the_desktop_app_completes_the_link_with_its_BEARER(app, monkeypatch):
    user = await _seed_user("deskok@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "deskok@acme.com")
        state = await _start_desktop_link(client, headers)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-ok", email="ok@github.com"),
        )
        done = await _callback(client, state)

        resp = await client.post(
            "/api/v1/auth/social/link/complete",
            json={"code": _link_code(done)},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "github"
    # The panel renders straight from this — the window that started the flow
    # has already closed, so a failed refetch would have nothing to recover.
    assert [i["provider"] for i in body["identities"]] == ["github"]
    assert body["identities"][0]["account_email"] == "ok@github.com"
    assert [a.account_id for a in (await User.get(user.id)).oauth_accounts] == ["gh-ok"]


async def test_a_parked_desktop_link_is_single_use(app, monkeypatch):
    await _seed_user("once@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "once@acme.com")
        state = await _start_desktop_link(client, headers)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-1x", email="1x@acme.com"),
        )
        done = await _callback(client, state)
        code = _link_code(done)

        first = await client.post(
            "/api/v1/auth/social/link/complete", json={"code": code}, headers=headers
        )
        second = await client.post(
            "/api/v1/auth/social/link/complete", json={"code": code}, headers=headers
        )

    assert first.status_code == 200
    assert second.status_code == 403


async def test_a_parked_link_redeemed_by_ANOTHER_account_attaches_nothing(app, monkeypatch):
    # THE security property of this path. The parked record names the account
    # the link was started for, so possession of the code is not enough.
    victim = await _seed_user("victim2@acme.com")
    await _seed_user("thief2@acme.com")

    async with _client(app) as victim_client:
        victim_headers = await _bearer(victim_client, "victim2@acme.com")
        state = await _start_desktop_link(victim_client, victim_headers)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-st", email="st@acme.com"),
        )
        done = await _callback(victim_client, state)
        stolen = _link_code(done)

    async with _client(app) as thief_client:
        thief_headers = await _bearer(thief_client, "thief2@acme.com")
        resp = await thief_client.post(
            "/api/v1/auth/social/link/complete",
            json={"code": stolen},
            headers=thief_headers,
        )

    assert resp.status_code == 403
    assert domain.REFUSE_LINK_SESSION_MISMATCH in resp.text
    assert (await User.get(victim.id)).oauth_accounts == []
    assert (await User.find_one(User.email == "thief2@acme.com")).oauth_accounts == []


async def test_completing_a_link_requires_a_session(app):
    # A code with no caller attached is worth nothing — which is what makes
    # parking it at the callback safe in the first place.
    async with _client(app) as anonymous:
        resp = await anonymous.post(
            "/api/v1/auth/social/link/complete", json={"code": "made-up"}
        )
    assert resp.status_code == 401


async def test_a_forged_link_code_is_refused(app):
    await _seed_user("forge@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "forge@acme.com")
        resp = await client.post(
            "/api/v1/auth/social/link/complete",
            json={"code": "not-a-real-code"},
            headers=headers,
        )
    assert resp.status_code == 403


async def test_the_desktop_policy_refusals_match_the_web_ones(app, monkeypatch):
    # Both clients share _apply_link_policy, and this is what stops the desktop
    # path quietly growing a weaker version of the takeover rule.
    identity = SocialIdentity(provider="github", account_id="gh-own2", email="o@acme.com")
    owner = await _seed_user("owner2@acme.com")
    async with _client(app) as owner_client:
        await _sign_in(owner_client, "owner2@acme.com")
        web_state = await _start_link(owner_client)
        _stub_exchange(monkeypatch, identity)
        await _callback(owner_client, web_state)

    await _seed_user("desktopthief@acme.com")
    async with _client(app) as thief:
        headers = await _bearer(thief, "desktopthief@acme.com")
        state = await _start_desktop_link(thief, headers)
        _stub_exchange(monkeypatch, identity)
        done = await _callback(thief, state)
        resp = await thief.post(
            "/api/v1/auth/social/link/complete",
            json={"code": _link_code(done)},
            headers=headers,
        )

    assert resp.status_code == 403
    assert domain.REFUSE_IDENTITY_CLAIMED in resp.text
    assert [a.account_id for a in (await User.get(owner.id)).oauth_accounts] == ["gh-own2"]


async def test_a_desktop_link_failure_lands_where_the_webview_can_close(app, monkeypatch):
    # A refusal that redirects to a web page leaves a Tauri window open with no
    # explanation. Provider/network failure is the one refusal that still
    # happens at the callback, so it has to target /oauth-callback too.
    from pocketpaw_ee.cloud.auth.social.providers.github import GitHubProvider

    await _seed_user("boom@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "boom@acme.com")
        state = await _start_desktop_link(client, headers)

        async def explode(self, **kwargs):  # noqa: ANN001, ARG001
            raise RuntimeError("github is down")

        monkeypatch.setattr(GitHubProvider, "exchange", explode)
        done = await _callback(client, state)

    location = done.headers["location"]
    assert location.startswith("http://localhost:1420/oauth-callback?"), location
    assert "link_error=social.callback_failed" in location
    # Not the sign-in dialog, which a webview cannot use.
    assert "auth=signin" not in location


async def test_a_hostile_next_cannot_reach_the_desktop_redirect(app, monkeypatch):
    # The desktop redirect is built from the frontend origin plus fixed params,
    # so `next` is never interpolated into it. Asserted rather than assumed:
    # this branch learned once already that two redirects do not inherit each
    # other's validation.
    await _seed_user("deskhostile@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "deskhostile@acme.com")
        resp = await client.post(
            "/api/v1/auth/social/github/link",
            params={"flow": "desktop", "next": "//evil.com"},
            headers=headers,
        )
        state = resp.json()["authorize_url"].split("state=")[1].split("&")[0]
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-hn", email="hn@acme.com"),
        )
        done = await _callback(client, state)

    location = done.headers["location"]
    assert location.startswith("http://localhost:1420/oauth-callback?"), location
    assert "evil.com" not in location


async def test_a_parked_link_code_cannot_be_spent_as_a_login_exchange_code(app, monkeypatch):
    # Different namespaces. One attaches an identity, the other mints a bearer;
    # making them interchangeable would turn a link into a login.
    await _seed_user("ns2@acme.com")
    async with _client(app) as client:
        headers = await _bearer(client, "ns2@acme.com")
        state = await _start_desktop_link(client, headers)
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-ns", email="ns2@acme.com"),
        )
        done = await _callback(client, state)

        resp = await client.post(
            "/api/v1/auth/social/exchange", json={"xc": _link_code(done)}
        )

    assert resp.status_code == 403


async def test_the_web_link_flow_is_unchanged_by_the_desktop_branch(app, monkeypatch):
    # flow defaults to web, and the web branch still attaches at the callback
    # off the cookie. Guards against the desktop work regressing the path that
    # already worked.
    user = await _seed_user("stillweb@acme.com")
    async with _client(app) as client:
        await _sign_in(client, "stillweb@acme.com")
        state = await _start_link(client, next_path="/settings/security")
        _stub_exchange(
            monkeypatch,
            SocialIdentity(provider="github", account_id="gh-sw", email="sw@acme.com"),
        )
        done = await _callback(client, state)

    location = done.headers["location"]
    assert location.startswith("http://localhost:1420/settings/security?")
    assert "social_linked=github" in location
    assert "oauth-callback" not in location
    assert [a.account_id for a in (await User.get(user.id)).oauth_accounts] == ["gh-sw"]
