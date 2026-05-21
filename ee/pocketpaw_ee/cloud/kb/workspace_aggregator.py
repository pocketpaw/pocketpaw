# workspace_aggregator.py — Workspace-level KB aggregator for Cluster C / PR 1.
# Created: 2026-04-19 — Powers GET /api/v1/knowledge/articles by merging the
# workspace-scoped KB (`workspace:{id}`) with every per-agent KB
# (`agent:{agent_id}`) that lives inside the same workspace. Scope checks are
# enforced by the route layer; this module is pure aggregation and sorting.
"""Workspace-level knowledge aggregator.

The `/api/v1/kb/articles` endpoint only lists articles at the workspace scope.
Per-agent knowledge (ingested via the agent Knowledge tab) lives in its own kb
scope (`agent:{agent_id}`) and today has no cross-agent browse path — the only
consumer is the per-agent search endpoint.

For the workspace-level KB browser (UI-TESTING-GUIDE §5) we need to flatten
both surfaces into a single stream, tagged with the owning scope so the UI can
filter by agent + source. This module encapsulates that merge so the route
handler stays thin and the merge is unit-testable without a live kb binary or
MongoDB.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AggregatedArticle:
    """One row in the workspace KB browser."""

    id: str
    title: str
    source: str
    scope: str  # "workspace:{id}" or "agent:{agent_id}"
    agent_id: str | None
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "scope": self.scope,
            "agent_id": self.agent_id,
            "updated_at": self.updated_at,
        }


def _row_to_article(row: Any, *, scope: str, agent_id: str | None) -> AggregatedArticle | None:
    """Normalise one kb-binary row into an AggregatedArticle.

    The kb binary returns JSON objects that vary by list/search call. We accept
    a few common key names so the aggregator survives minor CLI drift.
    """
    if not isinstance(row, dict):
        return None
    article_id = row.get("id") or row.get("article_id") or row.get("_id")
    if not article_id:
        return None
    title = row.get("title") or row.get("name") or row.get("source") or f"article:{article_id}"
    source = row.get("source") or row.get("origin") or ""
    updated_at = row.get("updated_at") or row.get("updatedAt") or row.get("modified")
    return AggregatedArticle(
        id=str(article_id),
        title=str(title),
        source=str(source),
        scope=scope,
        agent_id=agent_id,
        updated_at=str(updated_at) if updated_at else None,
    )


def _dedupe(articles: list[AggregatedArticle]) -> list[AggregatedArticle]:
    """Drop duplicates by (scope, id) — a single article can only belong to one
    scope but the kb binary occasionally returns the same row twice on boundary
    conditions. De-duping keeps the UI honest."""
    seen: set[tuple[str, str]] = set()
    out: list[AggregatedArticle] = []
    for article in articles:
        key = (article.scope, article.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(article)
    return out


def _sort_newest_first(articles: list[AggregatedArticle]) -> list[AggregatedArticle]:
    """Sort by updated_at desc with None last. Stable on ties so scope ordering
    is preserved from the caller."""
    return (
        sorted(
            articles,
            key=lambda a: (a.updated_at is None, a.updated_at or ""),
            reverse=False,
        )
        if not articles
        else sorted(
            articles,
            key=lambda a: a.updated_at or "",
            reverse=True,
        )
    )


async def aggregate_workspace_articles(
    *,
    workspace_id: str,
    agent_ids: list[str],
    kb_list: Callable[[str], Awaitable[list[Any]] | list[Any]],
    agent_filter: str | None = None,
) -> list[AggregatedArticle]:
    """Aggregate KB articles across the workspace scope + each agent scope.

    Parameters
    ----------
    workspace_id : str
        The active workspace id — maps to `workspace:{id}` kb scope.
    agent_ids : list[str]
        Every agent id that belongs to the workspace. The caller resolves these
        against the Agent collection with proper scope guards.
    kb_list : callable
        Function that takes a scope string and returns a list of kb rows. The
        real implementation wraps the kb-go binary's ``list`` command; tests
        substitute an in-memory dict.
    agent_filter : str | None
        If set, only include articles whose ``agent_id`` matches this value.
        ``"workspace"`` means workspace-scoped articles only; ``None`` means
        no filter.

    Returns
    -------
    list[AggregatedArticle]
        Newest-first, de-duplicated.
    """
    scopes: list[tuple[str, str | None]] = []
    if agent_filter is None or agent_filter == "workspace":
        scopes.append((f"workspace:{workspace_id}", None))
    if agent_filter != "workspace":
        for agent_id in agent_ids:
            if agent_filter is not None and agent_filter != agent_id:
                continue
            scopes.append((f"agent:{agent_id}", agent_id))

    articles: list[AggregatedArticle] = []
    for scope, agent_id in scopes:
        try:
            result = kb_list(scope)
            if hasattr(result, "__await__"):
                rows = await result  # type: ignore[assignment]
            else:
                rows = result
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb list failed for scope=%s: %s", scope, exc)
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            article = _row_to_article(row, scope=scope, agent_id=agent_id)
            if article is not None:
                articles.append(article)

    return _sort_newest_first(_dedupe(articles))
