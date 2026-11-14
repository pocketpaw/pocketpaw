# ee/pocketpaw_ee/cloud/growth/social/changes.py — what changed in a product's GitHub
# repos, as material for the daily X drafts.
#
# ``github_changes`` is the production ``ChangesFn``: for each ``owner/name`` slug it
# reads merged pull requests and published releases since a cutoff from the public
# GitHub REST API. A repo that fails (HTTP error, timeout, bad JSON) is logged and
# skipped; the others still return. Slugs that do not look like ``owner/name`` are
# skipped before any URL is built. An optional token comes from
# ``POCKETPAW_GROWTH_GITHUB_TOKEN`` and is never logged.
#
# ``changes_text`` is the one canonical rendering of a run's changes. The ideas prompt
# shows it to the model and ``ideas.check_draft`` checks every number in a draft
# against it, so the two can never disagree about what the source said.
#
# The ``set_production_changes_fn`` / ``resolve_changes_fn`` seam mirrors the ideas
# seam: nothing is installed by default, tests install a fake.

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# Fixed host, never user-supplied, so ``safe_fetch`` (SSRF guard) is not needed here.
GITHUB_API = "https://api.github.com"
TOKEN_ENV = "POCKETPAW_GROWTH_GITHUB_TOKEN"
EXCERPT_CHARS = 280
_SLUG_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class Change:
    kind: str  # "pr" or "release"
    title: str
    url: str
    excerpt: str


class ChangesFn(Protocol):
    async def __call__(self, repos: list[str], *, since: datetime) -> list[Change]: ...


def _excerpt(body: Any) -> str:
    return " ".join(str(body or "").split())[:EXCERPT_CHARS]


def _at_or_after(stamp: Any, since: datetime) -> bool:
    if not isinstance(stamp, str) or not stamp:
        return False
    return datetime.fromisoformat(stamp) >= since


def _change(kind: str, title: Any, item: dict[str, Any]) -> Change:
    url = str(item.get("html_url") or "")
    return Change(kind, str(title or ""), url, _excerpt(item.get("body")))


async def _repo_changes(
    client: httpx.AsyncClient, repo: str, since: datetime, headers: dict[str, str]
) -> list[Change]:
    base = f"{GITHUB_API}/repos/{repo}"
    pulls = await client.get(
        f"{base}/pulls",
        params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": 30},
        headers=headers,
    )
    pulls.raise_for_status()
    releases = await client.get(f"{base}/releases", params={"per_page": 10}, headers=headers)
    releases.raise_for_status()
    out: list[Change] = []
    for pr in pulls.json() or []:
        if isinstance(pr, dict) and _at_or_after(pr.get("merged_at"), since):
            out.append(_change("pr", pr.get("title"), pr))
    for rel in releases.json() or []:
        if isinstance(rel, dict) and not rel.get("draft"):
            if _at_or_after(rel.get("published_at"), since):
                out.append(_change("release", rel.get("name") or rel.get("tag_name"), rel))
    return out


async def github_changes(
    repos: list[str], *, since: datetime, client: httpx.AsyncClient | None = None
) -> list[Change]:
    """The production ``ChangesFn``. Never raises for one bad repo."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    out: list[Change] = []
    try:
        for repo in repos:
            if not _SLUG_RE.match(repo or ""):
                logger.warning("growth changes: skipping invalid repo slug %r", repo)
                continue
            try:
                out += await _repo_changes(client, repo, since, headers)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                logger.warning("growth changes: %s failed (%s)", repo, type(exc).__name__)
    finally:
        if own:
            await client.aclose()
    return out


def changes_text(changes: list[Change]) -> str:
    """One line per change: what the prompt shows and what drafts are checked against."""
    return "\n".join(f"- {c.kind}: {c.title} ({c.url}) {c.excerpt}".rstrip() for c in changes)


_PRODUCTION_CHANGES_FN: ChangesFn | None = None


def set_production_changes_fn(fn: ChangesFn | None) -> None:
    global _PRODUCTION_CHANGES_FN
    _PRODUCTION_CHANGES_FN = fn


def resolve_changes_fn() -> ChangesFn | None:
    return _PRODUCTION_CHANGES_FN


__all__ = [
    "Change",
    "ChangesFn",
    "changes_text",
    "github_changes",
    "resolve_changes_fn",
    "set_production_changes_fn",
]
