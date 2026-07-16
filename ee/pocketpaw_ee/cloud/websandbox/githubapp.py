# githubapp.py — GitHub App token client for the Web Cursor git-proxy broker (WC-6).
# Created 2026-07-16 (feat/code-mode).
#
# This is the "mint the token OUTSIDE the VM" half of WC-6's token-isolation
# guarantee. It turns the Paw GitHub App's private key into short-lived, single-
# repo, least-privilege installation tokens that the broker (githubapp → broker,
# a later slice) injects upstream when the VM's git remote talks to github.com.
# The token itself NEVER touches the sandbox — the VM sees only the broker URL.
#
# Two credentials, two scopes:
#   * App JWT — signed RS256 with the App private key, iss = App id, exp ≤ 10 min
#     (GitHub's hard cap). Authenticates AS THE APP to mint installation tokens.
#     Held only in memory for the length of one mint call.
#   * Installation token — minted per git operation, scoped to a SINGLE repo
#     (``repositories=[name]``) with least-privilege permissions (contents +
#     pull_requests), GitHub-lifetimed to ≤ 1h. This is what the broker injects
#     upstream; it is repo-scoped so a leak can't reach the tenant's other repos.
#
# The HTTP layer is injected (``http=``) so tests exercise the JWT + request
# shaping against a fake GitHub with no network and no real App. ``get_github_app_
# client()`` mirrors ``get_daytona_client()``: returns ``None`` when the App isn't
# configured, so callers degrade cleanly instead of crashing.
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.websandbox.repoauth import (
    ProviderId,
    RemoteRepo,
    ScopedRepoToken,
)

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
# The git host the broker proxies clone/fetch/push to (github.com for public
# GitHub; a GHES host for enterprise). Distinct from the REST api base.
_GITHUB_GIT_BASE = "https://github.com"

# GitHub caps the App JWT lifetime at 10 minutes; use 9 to leave headroom, and
# backdate ``iat`` 60s so minor clock skew between us and GitHub can't reject it.
_APP_JWT_TTL_SECONDS = 540
_APP_JWT_BACKDATE_SECONDS = 60

# Least-privilege default for a Web Cursor session: read/write the repo contents
# (clone / fetch / push) and open pull requests (WC-7). Nothing else — no admin,
# no secrets, no workflow scope.
_DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write"}


# ---------------------------------------------------------------------------
# Config (env), mirroring daytona/config.py.
# ---------------------------------------------------------------------------


def _app_id() -> str:
    return os.environ.get("POCKETPAW_GITHUB_APP_ID", "").strip()


def _private_key_pem() -> str:
    """The App private key as a PEM string.

    Accepts the raw PEM, a PEM with escaped ``\\n`` newlines (common in
    single-line env vars), or a base64-encoded PEM blob. Returns '' when unset.
    """
    raw = os.environ.get("POCKETPAW_GITHUB_APP_PRIVATE_KEY", "").strip()
    if not raw:
        return ""
    if "-----BEGIN" in raw:
        # Real PEM, possibly with literal ``\n`` sequences from a one-line env var.
        return raw.replace("\\n", "\n")
    # No PEM header → assume base64-encoded PEM.
    try:
        return base64.b64decode(raw).decode()
    except Exception:  # noqa: BLE001 — a malformed key is simply "not configured"
        logger.warning("POCKETPAW_GITHUB_APP_PRIVATE_KEY is set but not valid PEM/base64")
        return ""


def _api_base() -> str:
    return os.environ.get("GITHUB_API_BASE", _GITHUB_API_BASE).strip().rstrip("/")


def _git_base() -> str:
    return os.environ.get("GITHUB_GIT_BASE", _GITHUB_GIT_BASE).strip().rstrip("/")


def github_app_slug() -> str:
    """The App's public slug, for the install URL (``/apps/<slug>/installations/new``).

    Distinct from the numeric App id: GitHub's install page is keyed by the slug
    (e.g. ``devrohit06-personal``). Returns '' when unset — callers surface a clean
    "GitHub connect not configured" rather than building a broken URL.
    """
    return os.environ.get("POCKETPAW_GITHUB_APP_SLUG", "").strip()


def github_app_enabled() -> bool:
    """True when both the App id and a usable private key are configured."""
    return bool(_app_id() and _private_key_pem())


# ---------------------------------------------------------------------------
# Injected HTTP seam — httpx.AsyncClient satisfies it; tests pass a fake.
# ---------------------------------------------------------------------------


class _HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...

    @property
    def text(self) -> str: ...


class _HttpClient(Protocol):
    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any] | None = None
    ) -> _HttpResponse: ...

    async def get(self, url: str, *, headers: dict[str, str]) -> _HttpResponse: ...


@dataclass(frozen=True)
class InstallationToken:
    """A minted, repo-scoped installation token and its GitHub-set expiry."""

    token: str
    expires_at: datetime
    permissions: dict[str, str]
    repositories: tuple[str, ...]


class GitHubAppError(CloudError):
    """A GitHub App / installation-token exchange failed."""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(status, code, message)


class GitHubAppClient:
    """GitHub implementation of ``RepoAuthProvider`` for the git-proxy broker.

    Mints App JWTs and single-repo installation tokens (``connection_id`` is the
    GitHub App installation id). Stateless apart from its credentials + the
    injected HTTP client. Construct via ``get_github_app_client()`` in production;
    construct directly with a fake ``http`` in tests. The broker programs against
    the neutral ``RepoAuthProvider`` protocol, not this class directly.
    """

    provider_id: ProviderId = ProviderId.GITHUB

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        *,
        api_base: str = _GITHUB_API_BASE,
        git_base: str = _GITHUB_GIT_BASE,
        http: _HttpClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._api_base = api_base.rstrip("/")
        self._git_base = git_base.rstrip("/")
        self._http = http

    # -- RepoAuthProvider (neutral surface the broker uses) -------------------

    async def mint_repo_token(
        self,
        connection_id: str,
        repo: str,
        *,
        scopes: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> ScopedRepoToken:
        """Mint a JIT token scoped to a single ``repo`` (``owner/repo``).

        GitHub's access-token endpoint scopes by repo NAME within the
        installation's owner, so we pass the short name; the neutral token still
        carries the full ``owner/repo`` for the broker's clone URL.
        """
        installation = await self.mint_installation_token(
            connection_id,
            repositories=[_repo_short_name(repo)],
            permissions=scopes,
            now=now,
        )
        return ScopedRepoToken(
            provider=ProviderId.GITHUB,
            token=installation.token,
            expires_at=installation.expires_at,
            repo=repo,
            scopes=installation.permissions,
        )

    async def list_repositories(
        self, connection_id: str, *, now: datetime | None = None
    ) -> list[RemoteRepo]:
        """List the installation's repos as neutral ``RemoteRepo`` rows."""
        raw = await self.list_installation_repositories(connection_id, now=now)
        repos: list[RemoteRepo] = []
        for r in raw:
            full = r.get("full_name") or ""
            if not full:
                continue
            repos.append(
                RemoteRepo(
                    full_name=full,
                    private=bool(r.get("private", True)),
                    default_branch=r.get("default_branch") or "main",
                    clone_url=r.get("clone_url") or self.upstream_clone_url(full),
                )
            )
        return repos

    def upstream_clone_url(self, repo: str) -> str:
        """The github.com https URL the broker proxies to (never given to the VM)."""
        return f"{self._git_base}/{repo.removesuffix('.git')}.git"

    # -- App JWT --------------------------------------------------------------

    def app_jwt(self, *, now: datetime | None = None) -> str:
        """Mint a short-lived RS256 App JWT (iss=App id, exp ≤ 10 min).

        Deterministic under an injected ``now`` so tests can assert the claims.
        """
        moment = now or datetime.now(UTC)
        iat = int(moment.timestamp()) - _APP_JWT_BACKDATE_SECONDS
        exp = int(moment.timestamp()) + _APP_JWT_TTL_SECONDS
        payload = {"iat": iat, "exp": exp, "iss": self._app_id}
        try:
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except Exception as exc:  # noqa: BLE001 — a bad key is an operational error
            raise GitHubAppError(
                "websandbox.github_app_key_invalid",
                "The GitHub App private key could not sign a token",
            ) from exc

    # -- Installation token ---------------------------------------------------

    async def mint_installation_token(
        self,
        installation_id: str,
        *,
        repositories: list[str] | None = None,
        permissions: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> InstallationToken:
        """Mint a JIT installation token, scoped to ``repositories`` if given.

        Always pass a single repo name for a git operation — a repo-scoped token
        can't reach the tenant's other repos if it leaks. ``permissions`` defaults
        to least-privilege (contents + pull_requests). GitHub sets the expiry
        (≤ 1h); it's returned so the broker can refresh before it lapses.
        """
        perms = permissions or dict(_DEFAULT_PERMISSIONS)
        body: dict[str, Any] = {"permissions": perms}
        if repositories:
            body["repositories"] = repositories

        url = f"{self._api_base}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self.app_jwt(now=now)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._request("POST", url, headers=headers, json=body)
        if resp.status_code != 201:
            raise GitHubAppError(
                "websandbox.installation_token_failed",
                f"GitHub declined to mint an installation token (HTTP {resp.status_code})",
            )
        data = resp.json()
        token = data.get("token")
        if not token:
            raise GitHubAppError(
                "websandbox.installation_token_failed",
                "GitHub returned no installation token",
            )
        return InstallationToken(
            token=token,
            expires_at=_parse_expiry(data.get("expires_at")),
            permissions=data.get("permissions", perms),
            repositories=tuple(repositories or ()),
        )

    async def list_installation_repositories(
        self, installation_id: str, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """List the repos an installation can reach (for the repo picker).

        Uses a metadata-only installation token, minted + used server-side only —
        it never enters a VM. Returns the raw GitHub repo objects (full_name,
        private, default_branch, …) for the frontend to render.
        """
        meta = await self.mint_installation_token(
            installation_id, permissions={"metadata": "read"}, now=now
        )
        url = f"{self._api_base}/installation/repositories?per_page=100"
        headers = {
            "Authorization": f"token {meta.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._request("GET", url, headers=headers)
        if resp.status_code != 200:
            raise GitHubAppError(
                "websandbox.installation_repos_failed",
                f"Could not list installed repositories (HTTP {resp.status_code})",
            )
        return list(resp.json().get("repositories", []))

    # -- transport ------------------------------------------------------------

    async def _request(
        self, method: str, url: str, *, headers: dict[str, str], json: dict[str, Any] | None = None
    ) -> _HttpResponse:
        if self._http is not None:
            if method == "POST":
                return await self._http.post(url, headers=headers, json=json)
            return await self._http.get(url, headers=headers)
        # Lazy httpx per call — no shared session lifecycle to manage.
        import httpx

        async with httpx.AsyncClient(timeout=20.0) as client:
            if method == "POST":
                return await client.post(url, headers=headers, json=json)
            return await client.get(url, headers=headers)


def _repo_short_name(repo: str) -> str:
    """``owner/repo`` → ``repo`` (GitHub's access-token endpoint scopes by name)."""
    return repo.removesuffix(".git").split("/")[-1]


def _parse_expiry(value: Any) -> datetime:
    """Parse GitHub's ISO-8601 ``expires_at`` (``2026-07-16T10:00:00Z``).

    Falls back to now+1h (GitHub's max) if the field is missing/unparseable, so a
    caller always has a conservative refresh deadline.
    """
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC) + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Singleton factory, mirroring get_daytona_client().
# ---------------------------------------------------------------------------

_client: GitHubAppClient | None = None


def get_github_app_client() -> GitHubAppClient | None:
    """Return the singleton GitHub App client, or None if the App isn't configured."""
    global _client
    if not github_app_enabled():
        return None
    if _client is None:
        _client = GitHubAppClient(
            _app_id(), _private_key_pem(), api_base=_api_base(), git_base=_git_base()
        )
    return _client


def _reset_client_for_tests() -> None:
    """Drop the cached singleton (tests that flip env vars call this)."""
    global _client
    _client = None


__all__ = [
    "GitHubAppClient",
    "GitHubAppError",
    "InstallationToken",
    "get_github_app_client",
    "github_app_enabled",
]
