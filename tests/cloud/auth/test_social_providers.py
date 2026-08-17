"""Social provider adapters (AM-2 / AM-3) — the email-verification boundary.

Every test here defends one rule: an adapter may only report an email that its
provider actively vouches for. Getting this wrong is not a cosmetic bug, it is
account takeover — an attacker attaches victim@corp.com to their own provider
account, signs in, and the linking table hands them the victim's session.

The GitHub cases matter most, because GitHub exposes two email fields and the
obvious one is the wrong one: `/user`'s `email` is the public profile address,
self-asserted and unverified, while only `/user/emails` carries `verified`.
"""

import os

os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.auth.social.providers import (
    configured_providers,
    get_provider,
)
from pocketpaw_ee.cloud.auth.social.providers.base import SocialIdentity
from pocketpaw_ee.cloud.auth.social.providers.github import (
    GitHubProvider,
    pick_verified_email,
)
from pocketpaw_ee.cloud.auth.social.providers.google import (
    GoogleProvider,
    identity_from_claims,
)

# ---------------------------------------------------------------------------
# SocialIdentity
# ---------------------------------------------------------------------------


def test_identity_requires_a_provider_account_id():
    # account_id is the match key. An identity without one could only be
    # matched by email, which is the thing we refuse to do.
    with pytest.raises(ValueError):
        SocialIdentity(provider="google", account_id="", email="a@b.c")


def test_has_verified_email_is_false_without_an_email():
    ident = SocialIdentity(provider="github", account_id="1", email=None)
    assert ident.has_verified_email is False


def test_has_verified_email_is_true_with_one():
    ident = SocialIdentity(provider="github", account_id="1", email="a@b.c")
    assert ident.has_verified_email is True


# ---------------------------------------------------------------------------
# GitHub — pick_verified_email
# ---------------------------------------------------------------------------


def test_github_prefers_the_primary_verified_address():
    rows = [
        {"email": "alt@corp.com", "primary": False, "verified": True},
        {"email": "main@corp.com", "primary": True, "verified": True},
    ]
    assert pick_verified_email(rows) == "main@corp.com"


def test_github_falls_back_to_any_verified_address():
    rows = [
        {"email": "alt@corp.com", "primary": False, "verified": True},
        {"email": "main@corp.com", "primary": True, "verified": False},
    ]
    assert pick_verified_email(rows) == "alt@corp.com"


def test_github_NEVER_returns_an_unverified_address():
    # THE takeover case: the attacker adds the victim's address to their own
    # GitHub account. GitHub reports it, unverified. It must not come back.
    rows = [{"email": "victim@corp.com", "primary": True, "verified": False}]
    assert pick_verified_email(rows) is None


def test_github_ignores_a_primary_flag_on_an_unverified_row():
    rows = [
        {"email": "victim@corp.com", "primary": True, "verified": False},
        {"email": "attacker@evil.com", "primary": False, "verified": True},
    ]
    # Primary does not outrank verified.
    assert pick_verified_email(rows) == "attacker@evil.com"


def test_github_returns_none_for_an_empty_list():
    assert pick_verified_email([]) is None


def test_github_lowercases_the_address():
    rows = [{"email": "Main@Corp.COM", "primary": True, "verified": True}]
    assert pick_verified_email(rows) == "main@corp.com"


@pytest.mark.parametrize(
    "rows",
    [
        [{"email": "a@b.c", "primary": True, "verified": "true"}],  # string, not bool
        [{"email": "a@b.c", "primary": True, "verified": 1}],  # truthy, not True
        [{"email": "a@b.c", "primary": True}],  # absent
        [{"email": None, "primary": True, "verified": True}],  # no address
        ["not-a-dict"],
    ],
)
def test_github_treats_anything_but_boolean_true_as_unverified(rows):
    # `verified` is a real boolean in GitHub's response. A truthy string or 1
    # is not an assertion of verification, so it must not be read as one.
    assert pick_verified_email(rows) is None


# ---------------------------------------------------------------------------
# GitHub — the full exchange, against a stubbed API
# ---------------------------------------------------------------------------


def _github_transport(*, emails_status=200, emails_body=None, user_body=None, token_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login/oauth/access_token" in url:
            return httpx.Response(200, json=token_body or {"access_token": "gho_test"})
        if url.endswith("/user/emails"):
            if emails_status >= 400:
                return httpx.Response(emails_status, json={"message": "denied"})
            return httpx.Response(200, json=emails_body or [])
        if url.endswith("/user"):
            return httpx.Response(200, json=user_body or {"id": 42, "login": "octocat"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def github_env(monkeypatch):
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", "csecret")


async def _exchange(monkeypatch, transport) -> SocialIdentity:
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return await GitHubProvider().exchange(code="c", redirect_uri="https://app/cb")


async def test_github_exchange_returns_the_verified_address(monkeypatch, github_env):
    transport = _github_transport(
        emails_body=[{"email": "dev@corp.com", "primary": True, "verified": True}],
        user_body={"id": 7, "login": "octocat", "name": "Octo", "avatar_url": "http://a/x.png"},
    )
    ident = await _exchange(monkeypatch, transport)

    assert ident.provider == "github"
    assert ident.account_id == "7"
    assert ident.email == "dev@corp.com"
    assert ident.full_name == "Octo"


async def test_github_exchange_ignores_the_profile_email_field(monkeypatch, github_env):
    # /user carries a plausible-looking `email`. It is unverified, and
    # /user/emails says nothing is verified, so the result must be None.
    transport = _github_transport(
        emails_body=[{"email": "victim@corp.com", "primary": True, "verified": False}],
        user_body={"id": 7, "login": "octocat", "email": "victim@corp.com"},
    )
    ident = await _exchange(monkeypatch, transport)
    assert ident.email is None
    assert ident.account_id == "7"


async def test_github_declined_email_scope_yields_no_email_not_an_error(monkeypatch, github_env):
    # Declining `user:email` is a reasonable choice; it must degrade to the
    # refusal path, not a 500.
    transport = _github_transport(emails_status=403)
    ident = await _exchange(monkeypatch, transport)
    assert ident.email is None
    assert ident.account_id == "42"


async def test_github_surfaces_a_failed_token_exchange(monkeypatch, github_env):
    # GitHub reports exchange failures as HTTP 200 with an `error` key, so
    # raise_for_status alone would let this through.
    transport = _github_transport(
        token_body={"error": "bad_verification_code", "error_description": "expired"}
    )
    with pytest.raises(CloudError) as exc:
        await _exchange(monkeypatch, transport)
    assert exc.value.code == "social.github_token_exchange_failed"


async def test_github_rejects_a_profile_without_an_id(monkeypatch, github_env):
    transport = _github_transport(user_body={"login": "octocat"})
    with pytest.raises(CloudError) as exc:
        await _exchange(monkeypatch, transport)
    assert exc.value.code == "social.github_no_account_id"


def test_github_authorize_url_requests_user_email_scope(github_env):
    url = GitHubProvider().authorize_url(state="st", redirect_uri="https://app/cb")
    assert "user%3Aemail" in url or "user:email" in url
    assert "state=st" in url


def test_github_authorize_url_does_not_request_repo_scope(github_env):
    # Sign-in must never ask for repository permissions — that is the
    # codeconnect App's job, as a separate and later consent.
    url = GitHubProvider().authorize_url(state="st", redirect_uri="https://app/cb")
    assert "repo" not in url.split("scope=")[1].split("&")[0]


# ---------------------------------------------------------------------------
# Google — claims mapping
# ---------------------------------------------------------------------------


def test_google_accepts_a_verified_claim():
    ident = identity_from_claims(
        {"sub": "1234", "email": "Dev@Corp.com", "email_verified": True, "name": "Dev"}
    )
    assert ident.account_id == "1234"
    assert ident.email == "dev@corp.com"
    assert ident.full_name == "Dev"


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "1", "email": "v@corp.com", "email_verified": False},
        {"sub": "1", "email": "v@corp.com", "email_verified": "true"},  # string
        {"sub": "1", "email": "v@corp.com", "email_verified": 1},  # truthy int
        {"sub": "1", "email": "v@corp.com"},  # claim absent
        {"sub": "1", "email": "v@corp.com", "email_verified": None},
    ],
)
def test_google_refuses_anything_but_boolean_true(claims):
    # nOAuth is exactly this: a mutable, unverified email claim read as an
    # identity. Only a real boolean True counts.
    assert identity_from_claims(claims).email is None


def test_google_still_returns_the_identity_when_the_email_is_unusable():
    # The account_id is valid, so the user CAN still be signed in if this
    # provider account is already linked — only NEW linking is blocked.
    ident = identity_from_claims({"sub": "1", "email": "v@corp.com", "email_verified": False})
    assert ident.account_id == "1"
    assert ident.has_verified_email is False


def test_google_requires_a_sub_claim():
    with pytest.raises(CloudError) as exc:
        identity_from_claims({"email": "a@b.c", "email_verified": True})
    assert exc.value.code == "social.google_no_subject"


def test_google_authorize_url_carries_pkce_and_nonce(monkeypatch):
    monkeypatch.setenv("POCKETPAW_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("POCKETPAW_GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    url = GoogleProvider().authorize_url(
        state="st", redirect_uri="https://app/cb", code_challenge="chal", nonce="n1"
    )
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "nonce=n1" in url


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_resolves_both_providers():
    assert get_provider("google").name == "google"
    assert get_provider("github").name == "github"


def test_registry_is_case_and_space_insensitive():
    assert get_provider("  GitHub ").name == "github"


def test_registry_returns_none_for_an_unknown_provider():
    assert get_provider("myspace") is None
    assert get_provider("") is None


def test_unconfigured_providers_are_not_offered(monkeypatch):
    for var in (
        "POCKETPAW_GOOGLE_OAUTH_CLIENT_ID",
        "POCKETPAW_GOOGLE_OAUTH_CLIENT_SECRET",
        "POCKETPAW_GITHUB_OAUTH_CLIENT_ID",
        "POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    assert configured_providers() == []


def test_a_half_configured_provider_is_not_offered(monkeypatch):
    # An id with no secret would render a button that dies at the consent
    # screen. Both halves or nothing.
    monkeypatch.setenv("POCKETPAW_GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.delenv("POCKETPAW_GITHUB_OAUTH_CLIENT_SECRET", raising=False)
    assert "github" not in configured_providers()
