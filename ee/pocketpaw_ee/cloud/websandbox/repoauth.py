# repoauth.py — provider-agnostic repo-auth seam for the Web Cursor broker (WC-6).
# Created 2026-07-16 (feat/code-mode).
#
# Web Cursor clones a user's PRIVATE repos into a sandbox through a git-proxy
# broker that mints short-lived, repo-scoped tokens OUTSIDE the VM. GitHub App is
# the first provider, but auth is expanding to Google (and others), so the broker
# depends on THIS neutral interface — never on ``githubapp`` directly. A new
# provider is a sibling implementation of ``RepoAuthProvider`` + a branch in
# ``get_repo_auth_provider``; nothing in the broker, Registry, or connect flow
# needs to change.
#
# The neutral vocabulary:
#   * ProviderId       — which code host ("github", "google", …).
#   * connection_id    — the per-tenant handle the provider issues at connect
#                        time (a GitHub App installation id; a Google OAuth
#                        connection id). Stored (encrypted) in the WC-1 Registry.
#   * ScopedRepoToken  — a JIT, single-repo, short-lived credential the broker
#                        injects upstream. Never enters the VM.
#   * RemoteRepo       — one repo the connection can reach, for the picker.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderId(StrEnum):
    """The code host a connection speaks to. Extend as providers land."""

    GITHUB = "github"
    GOOGLE = "google"  # planned — no implementation yet


@dataclass(frozen=True)
class ScopedRepoToken:
    """A JIT credential scoped to ONE repo, minted outside the VM.

    ``token`` is what the broker injects upstream; ``expires_at`` is the
    provider-set deadline (≤ 1h) the broker refreshes before. Repo-scoped so a
    leak can't reach the tenant's other repos.
    """

    provider: ProviderId
    token: str
    expires_at: datetime
    repo: str
    scopes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteRepo:
    """One repo a connection can reach — the shape the repo picker renders."""

    full_name: str  # "owner/repo" (github) / "project/repo" (google), provider-native
    private: bool
    default_branch: str
    clone_url: str  # the provider-native https clone URL the broker proxies to


@runtime_checkable
class RepoAuthProvider(Protocol):
    """What the git-proxy broker needs from any code-host auth provider.

    Concrete implementations: ``githubapp.GitHubAppClient`` (today); a Google
    provider next. The broker programs against this, so adding a provider never
    touches the broker.
    """

    provider_id: ProviderId

    async def mint_repo_token(
        self,
        connection_id: str,
        repo: str,
        *,
        scopes: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> ScopedRepoToken:
        """Mint a JIT token scoped to a single ``repo`` under ``connection_id``."""
        ...

    async def list_repositories(
        self, connection_id: str, *, now: datetime | None = None
    ) -> list[RemoteRepo]:
        """List the repos ``connection_id`` can reach (for the picker)."""
        ...

    def upstream_clone_url(self, repo: str) -> str:
        """The provider-native https URL the broker proxies clone/fetch/push to."""
        ...


def get_repo_auth_provider(provider: ProviderId | str) -> RepoAuthProvider | None:
    """Resolve the configured provider, or None when it isn't set up.

    Mirrors ``get_daytona_client()``: a missing/unconfigured provider returns
    None so callers degrade cleanly instead of crashing.
    """
    kind = ProviderId(provider) if not isinstance(provider, ProviderId) else provider
    if kind is ProviderId.GITHUB:
        from pocketpaw_ee.cloud.websandbox.githubapp import get_github_app_client

        return get_github_app_client()
    # ProviderId.GOOGLE and future providers: not implemented yet.
    return None


__all__ = [
    "ProviderId",
    "RemoteRepo",
    "RepoAuthProvider",
    "ScopedRepoToken",
    "get_repo_auth_provider",
]
