# test_websandbox_githubapp.py — GitHub App token client + provider seam (WC-6).
# Created 2026-07-16 (feat/code-mode).
#
# Verifies the "mint the token OUTSIDE the VM" half of WC-6's token isolation
# without a live App: a generated RSA keypair signs the App JWT, and a fake
# GitHub captures the request shaping. Locks the security-load-bearing
# properties — single-repo scoping, least-privilege permissions, ≤10-min App JWT,
# provider-agnostic surface — that the git-proxy broker will lean on.
from __future__ import annotations

import json
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pocketpaw_ee.cloud.websandbox.githubapp import (
    GitHubAppClient,
    GitHubAppError,
    get_github_app_client,
    github_app_enabled,
    github_app_slug,
)
from pocketpaw_ee.cloud.websandbox.githubapp import (
    _reset_client_for_tests as reset_github_client,
)
from pocketpaw_ee.cloud.websandbox.repoauth import (
    ProviderId,
    RepoAuthProvider,
    ScopedRepoToken,
    get_repo_auth_provider,
)

# One RSA keypair for the whole module — 2048-bit gen is ~100ms, do it once.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_PUBLIC_PEM = (
    _KEY.public_key()
    .public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)

_FIXED_NOW = datetime(2026, 7, 16, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake GitHub HTTP.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class _FakeHttp:
    """Captures requests, matches a canned response by URL substring."""

    def __init__(self, responses: dict[str, _FakeResp]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def post(self, url, *, headers, json=None):  # noqa: ANN001
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self._match(url)

    async def get(self, url, *, headers):  # noqa: ANN001
        self.calls.append({"method": "GET", "url": url, "headers": headers, "json": None})
        return self._match(url)

    def _match(self, url: str) -> _FakeResp:
        for key, resp in self.responses.items():
            if key in url:
                return resp
        raise AssertionError(f"no fake response for {url}")


def _token_resp() -> _FakeResp:
    return _FakeResp(
        201,
        {
            "token": "ghs_installationtoken",
            "expires_at": "2026-07-16T10:00:00Z",
            "permissions": {"contents": "write", "pull_requests": "write"},
        },
    )


def _client(http: _FakeHttp) -> GitHubAppClient:
    return GitHubAppClient("123456", _PRIVATE_PEM, http=http)


# ---------------------------------------------------------------------------
# App JWT.
# ---------------------------------------------------------------------------


def test_app_jwt_claims_are_signed_and_within_github_limits() -> None:
    client = _client(_FakeHttp({}))
    token = client.app_jwt(now=_FIXED_NOW)
    claims = jwt.decode(token, _PUBLIC_PEM, algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "123456"
    # iat is backdated for skew; exp - iat must not exceed GitHub's 10-min cap.
    assert claims["iat"] < int(_FIXED_NOW.timestamp())
    assert claims["exp"] - claims["iat"] <= 600


def test_app_jwt_invalid_key_raises_clean_error() -> None:
    client = GitHubAppClient("123456", "-----BEGIN PRIVATE KEY-----\nnope\n", http=_FakeHttp({}))
    with pytest.raises(GitHubAppError) as exc:
        client.app_jwt(now=_FIXED_NOW)
    assert exc.value.code == "websandbox.github_app_key_invalid"


# ---------------------------------------------------------------------------
# Installation token — single-repo scope, least privilege.
# ---------------------------------------------------------------------------


async def test_mint_repo_token_scopes_to_single_repo_least_privilege() -> None:
    http = _FakeHttp({"access_tokens": _token_resp()})
    client = _client(http)

    tok = await client.mint_repo_token("inst-1", "acme/api", now=_FIXED_NOW)

    # Neutral token carries the full repo + provider + expiry.
    assert isinstance(tok, ScopedRepoToken)
    assert tok.provider is ProviderId.GITHUB
    assert tok.token == "ghs_installationtoken"
    assert tok.repo == "acme/api"
    assert tok.expires_at == datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)

    # The request scoped to exactly ONE repo (short name) with default
    # least-privilege permissions — never installation-wide.
    call = http.calls[0]
    assert call["url"].endswith("/app/installations/inst-1/access_tokens")
    assert call["json"]["repositories"] == ["api"]
    assert call["json"]["permissions"] == {"contents": "write", "pull_requests": "write"}
    assert call["headers"]["Authorization"].startswith("Bearer ")


async def test_mint_repo_token_honors_explicit_scopes() -> None:
    http = _FakeHttp({"access_tokens": _token_resp()})
    client = _client(http)
    await client.mint_repo_token(
        "inst-1", "acme/api", scopes={"contents": "read"}, now=_FIXED_NOW
    )
    assert http.calls[0]["json"]["permissions"] == {"contents": "read"}


async def test_mint_installation_token_error_on_non_201() -> None:
    http = _FakeHttp({"access_tokens": _FakeResp(404, {"message": "Not Found"})})
    client = _client(http)
    with pytest.raises(GitHubAppError) as exc:
        await client.mint_repo_token("inst-1", "acme/api", now=_FIXED_NOW)
    assert exc.value.code == "websandbox.installation_token_failed"


async def test_mint_installation_token_error_when_token_missing() -> None:
    http = _FakeHttp({"access_tokens": _FakeResp(201, {"expires_at": "2026-07-16T10:00:00Z"})})
    client = _client(http)
    with pytest.raises(GitHubAppError):
        await client.mint_repo_token("inst-1", "acme/api", now=_FIXED_NOW)


# ---------------------------------------------------------------------------
# Pull request (WC-7/P4b) + default branch.
# ---------------------------------------------------------------------------


async def test_create_pull_request_posts_and_returns_url_and_number() -> None:
    http = _FakeHttp(
        {
            "access_tokens": _token_resp(),  # repo-scoped token minted first
            "/pulls": _FakeResp(
                201,
                {"html_url": "https://github.com/acme/api/pull/7", "number": 7},
            ),
        }
    )
    client = _client(http)

    result = await client.create_pull_request(
        "inst-1",
        "acme/api",
        head="paw/edit-abc",
        base="main",
        title="Ship the thing",
        body="does the thing",
        now=_FIXED_NOW,
    )

    assert result == {"url": "https://github.com/acme/api/pull/7", "number": 7}
    # The token was minted repo-scoped with contents+pull_requests write.
    mint = http.calls[0]
    assert mint["json"]["repositories"] == ["api"]
    assert mint["json"]["permissions"] == {"contents": "write", "pull_requests": "write"}
    # The PR POST hit the right URL/body/headers.
    pr = http.calls[1]
    assert pr["method"] == "POST"
    assert pr["url"].endswith("/repos/acme/api/pulls")
    assert pr["json"] == {
        "title": "Ship the thing",
        "head": "paw/edit-abc",
        "base": "main",
        "body": "does the thing",
    }
    assert pr["headers"]["Authorization"] == "token ghs_installationtoken"
    assert pr["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


async def test_create_pull_request_422_surfaces_github_message() -> None:
    http = _FakeHttp(
        {
            "access_tokens": _token_resp(),
            "/pulls": _FakeResp(
                422,
                {
                    "message": "Validation Failed",
                    "errors": [{"message": "No commits between main and paw/edit-abc"}],
                },
            ),
        }
    )
    client = _client(http)

    with pytest.raises(GitHubAppError) as exc:
        await client.create_pull_request(
            "inst-1", "acme/api", head="paw/edit-abc", base="main",
            title="t", body="", now=_FIXED_NOW,
        )
    assert exc.value.status_code == 422
    assert "No commits between" in exc.value.message


async def test_create_pull_request_other_error_is_clean() -> None:
    http = _FakeHttp(
        {"access_tokens": _token_resp(), "/pulls": _FakeResp(500, {"message": "boom"})}
    )
    client = _client(http)
    with pytest.raises(GitHubAppError) as exc:
        await client.create_pull_request(
            "inst-1", "acme/api", head="h", base="main", title="t", now=_FIXED_NOW
        )
    assert exc.value.code == "websandbox.pr_failed"


async def test_get_default_branch_returns_repo_default() -> None:
    http = _FakeHttp(
        {
            "access_tokens": _token_resp(),
            "/repos/acme/api": _FakeResp(200, {"default_branch": "trunk"}),
        }
    )
    client = _client(http)
    assert await client.get_default_branch("inst-1", "acme/api", now=_FIXED_NOW) == "trunk"


async def test_get_default_branch_unreachable_repo_raises() -> None:
    http = _FakeHttp(
        {
            "access_tokens": _token_resp(),
            "/repos/acme/api": _FakeResp(404, {"message": "Not Found"}),
        }
    )
    client = _client(http)
    with pytest.raises(GitHubAppError):
        await client.get_default_branch("inst-1", "acme/api", now=_FIXED_NOW)


# ---------------------------------------------------------------------------
# Repo listing (picker) + upstream URL.
# ---------------------------------------------------------------------------


async def test_list_repositories_maps_to_neutral_rows() -> None:
    repos_payload = {
        "repositories": [
            {
                "full_name": "acme/api",
                "private": True,
                "default_branch": "main",
                "clone_url": "https://github.com/acme/api.git",
            },
            {"full_name": "acme/site", "private": False, "default_branch": "trunk"},
        ]
    }
    http = _FakeHttp(
        {
            "access_tokens": _token_resp(),  # metadata token minted first
            "installation/repositories": _FakeResp(200, repos_payload),
        }
    )
    client = _client(http)

    repos = await client.list_repositories("inst-1", now=_FIXED_NOW)

    assert [r.full_name for r in repos] == ["acme/api", "acme/site"]
    assert repos[0].private is True
    # Missing clone_url falls back to the derived github.com URL.
    assert repos[1].clone_url == "https://github.com/acme/site.git"
    # The picker token was minted metadata-only, not contents/write.
    assert http.calls[0]["json"]["permissions"] == {"metadata": "read"}


def test_upstream_clone_url_normalizes_suffix() -> None:
    client = _client(_FakeHttp({}))
    assert client.upstream_clone_url("acme/api") == "https://github.com/acme/api.git"
    assert client.upstream_clone_url("acme/api.git") == "https://github.com/acme/api.git"


# ---------------------------------------------------------------------------
# Installation account (connected-account chip display info).
# ---------------------------------------------------------------------------


async def test_get_installation_account_returns_login_and_avatar() -> None:
    http = _FakeHttp(
        {
            "/app/installations/inst-1": _FakeResp(
                200,
                {"account": {"login": "octo", "avatar_url": "https://av/o.png"}},
            )
        }
    )
    client = _client(http)

    info = await client.get_installation_account("inst-1", now=_FIXED_NOW)

    assert info == {"login": "octo", "avatar_url": "https://av/o.png"}
    # Read with the App JWT (no installation token needed for this metadata).
    assert http.calls[0]["headers"]["Authorization"].startswith("Bearer ")


async def test_get_installation_account_none_on_error_or_missing_account() -> None:
    # A non-200 → None (best-effort display enrichment, never an error).
    err = _client(_FakeHttp({"/app/installations/inst-1": _FakeResp(404, {"message": "gone"})}))
    assert await err.get_installation_account("inst-1", now=_FIXED_NOW) is None

    # A 200 with no login → None (nothing worth displaying).
    empty = _client(_FakeHttp({"/app/installations/inst-1": _FakeResp(200, {"account": {}})}))
    assert await empty.get_installation_account("inst-1", now=_FIXED_NOW) is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("devrohit06-personal", "devrohit06-personal"),  # the bare slug
        ("  devrohit06-personal  ", "devrohit06-personal"),  # whitespace
        # A pasted full install URL must resolve to the bare slug, not double-wrap.
        ("https://github.com/apps/devrohit06-personal", "devrohit06-personal"),
        ("https://github.com/apps/devrohit06-personal/installations/new", "devrohit06-personal"),
        ("", ""),  # unset → empty (caller shows "not configured")
    ],
)
def test_github_app_slug_extracts_bare_slug(monkeypatch, env: str, expected: str) -> None:
    monkeypatch.setenv("POCKETPAW_GITHUB_APP_SLUG", env)
    assert github_app_slug() == expected


# ---------------------------------------------------------------------------
# Provider-agnostic seam.
# ---------------------------------------------------------------------------


def test_github_client_satisfies_repo_auth_provider_protocol() -> None:
    client = _client(_FakeHttp({}))
    assert isinstance(client, RepoAuthProvider)
    assert client.provider_id is ProviderId.GITHUB


def test_google_provider_not_implemented_yet() -> None:
    # The seam exists; the implementation does not — resolve to None, never crash.
    assert get_repo_auth_provider(ProviderId.GOOGLE) is None


def test_factory_and_resolver_disabled_without_config(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("POCKETPAW_GITHUB_APP_PRIVATE_KEY", raising=False)
    reset_github_client()
    assert github_app_enabled() is False
    assert get_github_app_client() is None
    assert get_repo_auth_provider(ProviderId.GITHUB) is None


def test_factory_enabled_with_config(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_GITHUB_APP_ID", "123456")
    monkeypatch.setenv("POCKETPAW_GITHUB_APP_PRIVATE_KEY", _PRIVATE_PEM)
    reset_github_client()
    try:
        assert github_app_enabled() is True
        client = get_github_app_client()
        assert client is not None
        assert get_repo_auth_provider("github") is client
    finally:
        reset_github_client()


def test_private_key_accepts_escaped_newlines(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_GITHUB_APP_ID", "123456")
    monkeypatch.setenv("POCKETPAW_GITHUB_APP_PRIVATE_KEY", _PRIVATE_PEM.replace("\n", "\\n"))
    reset_github_client()
    try:
        client = get_github_app_client()
        assert client is not None
        # A JWT that verifies proves the escaped-newline PEM parsed correctly.
        claims = jwt.decode(client.app_jwt(now=_FIXED_NOW), _PUBLIC_PEM, algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
        assert claims["iss"] == "123456"
    finally:
        reset_github_client()
