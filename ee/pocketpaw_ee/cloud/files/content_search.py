# content_search.py — search INSIDE the caller's files, not across filenames.
#
# Created: 2026-08-29 (T3 "Files content search"). The Files panel's search box
# matched filenames only, client-side, over the loaded page. A file whose full
# text and summary we already hold was invisible unless its NAME matched. This
# module closes that: it asks kb-go what the caller's own KB scopes contain,
# then resolves the hits back to FILE rows through the ``kb_article_id``
# tracking the FileReady listener already writes (FL-11b).
#
# Why a new surface on /files instead of extending POST /kb/search:
#   * /kb/search answers a DIFFERENT question. It returns kb ARTICLES —
#     {id, title, summary, concepts} — which are compiled derivatives, not
#     files. The Files panel needs rows it can select, preview, download and
#     open an agent from, i.e. the ``UnifiedFile`` shape ``GET /files``
#     already returns. Widening /kb/search to also emit file rows would give
#     one endpoint two response shapes chosen by a flag, and callers would
#     have to know which one they got.
#   * The two have different tenancy stories. /kb/search takes a CLIENT scope
#     override bound to the caller by ``kb.service.validate_scope_override``.
#     A files search must not accept a scope at all — its scopes are DERIVED
#     from the caller (``_kb_scopes_for_context``) and its pocket partition is
#     the files ACL (``pockets_service.is_member``, the same 403 the listing
#     raises). Accepting both a kb scope and a pocket id on one route means
#     two ACLs on one door.
# What IS reused, deliberately: the kb machinery underneath (the same ``_kb``
# subprocess wrapper), the same ``_kb_scopes_for_context`` precedence
# (user > pocket > agent > workspace) the chat path uses, the same
# ``pockets_service.is_member`` gate, and the same
# ``files/service.unified_from_record`` projection the listing uses. No new
# ACL, no new index, no embeddings — kb-go scores 95% R@5 on LongMemEval with
# pure BM25, so this is wiring, not retrieval research.
#
# One subprocess per query: kb-go's ``search --scope "a,b,c"`` takes a
# comma-joined scope list and stamps each hit with the scope it came from, so
# the whole fan-out is a single call regardless of how many scopes the caller
# can read.

"""Files-scoped content search — kb-go hits resolved back to file rows."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pocketpaw_ee.cloud.files.service import UnifiedFile, unified_from_record
from pocketpaw_ee.cloud.uploads.mongo_store import LIST_WORKSPACE_ONLY, MongoFileStore

logger = logging.getLogger(__name__)


# The path segment of the content-search route. Declared ONCE here; the router
# decorates with it and the route test asserts the composed literal
# ``/api/v1/files/search`` off the mounted app, so a rename can't quietly move
# the endpoint out from under a client that pinned the old string.
CONTENT_SEARCH_ROUTE = "/search"

# ``degraded`` values on the response. ``None`` means the search ran normally.
DEGRADED_KB_UNAVAILABLE = "kb_unavailable"
"""kb-go could not be reached (missing binary, timeout, non-zero exit).
Content search is OFF, not empty — the UI must say so rather than render an
empty result set that reads as 'nothing in your files matches'."""

DEGRADED_VERBATIM = "verbatim"
"""At least one returned hit is an UNCOMPILED (verbatim-fallback) article, so
matching against it is literal text rather than a compiled summary. kb-go
marks these ``compiled_with == "none (fallback)"``. The 2026-08-04 ingest
funnel rejects them, but FL-11b tracking predates that hardening: rows ingested
on a pre-hardening keyless box recorded an article id for an article kb had
stored verbatim, so these still exist in the wild."""

_FALLBACK_COMPILED_WITH = "none (fallback)"

# kb search ranks the whole scope; only some hits are FILES (an article may
# have come from a URL ingest, a chat memory, a wiki page). Over-fetch so a
# page of file matches is still reachable when the top hits aren't files.
_OVERFETCH = 4
_MAX_KB_LIMIT = 100

# ``compiled_with`` is a property of the CORPUS, not of the query, so the probe
# that reads it is cached per scope. Five minutes is long enough that a
# debounced search box doesn't re-shell kb on every keystroke and short enough
# that a re-ingest clears the banner within one coffee.
_COMPILED_WITH_TTL_S = 300.0
_compiled_with_cache: dict[str, tuple[float, dict[str, str]]] = {}


def reset_compiled_with_cache() -> None:
    """Drop the per-scope ``compiled_with`` cache (tests; operator recovery)."""
    _compiled_with_cache.clear()


KbSearchFn = Callable[[str, list[str], int], Awaitable[list[dict[str, Any]]]]
KbListFn = Callable[[str], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class ContentMatch:
    """One file row plus WHY it matched."""

    file: UnifiedFile
    article_id: str
    scope: str
    title: str
    snippet: str
    verbatim: bool

    def to_json(self) -> dict[str, Any]:
        """The listing row, plus a ``match`` block explaining the hit.

        Spreading ``file.to_json()`` (rather than rebuilding the row) is the
        point: a field added to the flat listing shows up here for free. The
        panel renders these with the same components as a listing row.
        """
        return {
            **self.file.to_json(),
            "match": {
                "article_id": self.article_id,
                "scope": self.scope,
                "title": self.title,
                "snippet": self.snippet,
                "verbatim": self.verbatim,
            },
        }


@dataclass(frozen=True)
class ContentSearchResult:
    matches: list[ContentMatch]
    scopes: list[str]
    degraded: str | None


def resolve_kb_scopes(*, workspace_id: str, user_id: str, pocket_id: str | None) -> list[str]:
    """KB scopes this caller may read, most-specific first.

    Delegates to ``chat.agent_service._kb_scopes_for_context`` — the SAME
    precedence (user > pocket > agent > workspace) and the same
    member-private ``user:`` gate the chat path uses. A files search is a
    member's own solo read, so ``members=[user_id]`` is the truthful context
    and the gate emits the caller's private scope; it can never emit anyone
    else's. There is no target agent on this surface, so no ``agent:`` scope.

    Imported inside the function: ``chat.agent_service`` is a large module
    that reaches back into files/uploads, and the ``pockets_service`` import
    in ``files/router.list_files`` is the local precedent for keeping that
    edge out of module import order.
    """
    from pocketpaw_ee.cloud.chat.agent_service import (
        ScopeContext,
        ScopeKind,
        _kb_scopes_for_context,
    )

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id=f"files:{workspace_id}",
        workspace_id=workspace_id,
        user_id=user_id,
        members=[user_id],
        target_agent_id="",
        pocket_id=pocket_id,
    )
    return _kb_scopes_for_context(ctx)


async def _default_kb_search(query: str, scopes: list[str], limit: int) -> list[dict[str, Any]]:
    """One ``kb search`` over every scope at once, off the event loop.

    ``asyncio.to_thread`` is not optional: ``_kb`` is a blocking
    ``subprocess.run``. ``POST /kb/search`` calls it inline and stalls the
    whole loop for the duration; the 2026-08-04 hardening fixed that for
    ingest and left search alone. A debounced search box would make that
    stall per keystroke.
    """
    from pocketpaw_ee.cloud.agents.knowledge import _kb

    results = await asyncio.to_thread(
        _kb,
        "search",
        query,
        "--scope",
        ",".join(scopes),
        "--limit",
        str(limit),
    )
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


async def _default_kb_list(scope: str) -> list[dict[str, Any]]:
    """``kb list`` for one scope — the only kb command that emits
    ``compiled_with`` (search does not), which is how a verbatim article is
    identified."""
    from pocketpaw_ee.cloud.agents.knowledge import _kb

    rows = await asyncio.to_thread(_kb, "list", "--scope", scope)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def _compiled_with_for_scope(scope: str, kb_list: KbListFn | None) -> dict[str, str]:
    """``{article_id: compiled_with}`` for one scope, TTL-cached.

    The cache is bypassed entirely when a probe is injected, so a test never
    inherits another test's corpus. A probe failure returns ``{}`` — an
    unreadable ``compiled_with`` must never fail the search that already
    succeeded; it only means we can't claim the results are compiled.
    """
    use_cache = kb_list is None
    now = time.monotonic()
    if use_cache:
        cached = _compiled_with_cache.get(scope)
        if cached and cached[0] > now:
            return cached[1]
    probe = kb_list or _default_kb_list
    try:
        rows = await probe(scope)
    except Exception as exc:  # pragma: no cover — exercised via the fake probe
        logger.debug("kb list probe failed for scope %s: %s", scope, exc)
        return {}
    table = {
        str(r.get("id")): str(r.get("compiled_with") or "")
        for r in rows
        if isinstance(r, dict) and r.get("id")
    }
    if use_cache:
        _compiled_with_cache[scope] = (now + _COMPILED_WITH_TTL_S, table)
    return table


async def search_file_contents(
    *,
    workspace_id: str,
    user_id: str,
    query: str,
    limit: int = 20,
    pocket_id: str | None = None,
    kb_search: KbSearchFn | None = None,
    kb_list: KbListFn | None = None,
    store: MongoFileStore | None = None,
) -> ContentSearchResult:
    """Search the caller's KB scopes and return the FILE rows behind the hits.

    Ordering is kb-go's BM25 rank, preserved: hits are walked in the order
    kb returned them and each resolved row is appended once. Files that were
    never ingested (no ``kb_article_id``) simply cannot appear — that is the
    honest limit of this search, not a bug in it.

    ``kb_search`` / ``kb_list`` / ``store`` are injection seams for tests; the
    defaults shell out to kb-go and read Mongo.
    """
    q = (query or "").strip()
    scopes = resolve_kb_scopes(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
    if not q or not scopes:
        return ContentSearchResult(matches=[], scopes=scopes, degraded=None)

    capped = max(1, min(limit, 50))
    searcher = kb_search or _default_kb_search
    try:
        hits = await searcher(q, scopes, min(capped * _OVERFETCH, _MAX_KB_LIMIT))
    except Exception as exc:
        logger.warning("files content search: kb unreachable (scopes=%s): %s", scopes, exc)
        return ContentSearchResult(matches=[], scopes=scopes, degraded=DEGRADED_KB_UNAVAILABLE)

    # kb stamps ``scope`` on a hit only in multi-scope mode; with one scope the
    # answer is unambiguous, so fill it in rather than leaving the row blank.
    sole_scope = scopes[0] if len(scopes) == 1 else ""
    ordered: list[tuple[str, str, str, str]] = []  # (article_id, scope, title, snippet)
    for hit in hits:
        article_id = str(hit.get("id") or "")
        if not article_id:
            continue
        ordered.append(
            (
                article_id,
                str(hit.get("scope") or sole_scope),
                str(hit.get("title") or ""),
                str(hit.get("summary") or ""),
            )
        )
    if not ordered:
        return ContentSearchResult(matches=[], scopes=scopes, degraded=None)

    uploads = store or MongoFileStore()
    tracked = await uploads.list_by_kb_articles(
        workspace_id,
        [a for a, _s, _t, _n in ordered],
        # Tri-state, mirroring the listing: a pocket search sees that pocket's
        # rows; a workspace search sees workspace-scoped rows ONLY, so pocket
        # files never bleed into the workspace panel through this door.
        pocket_id=pocket_id if pocket_id else LIST_WORKSPACE_ONLY,
    )
    by_article: dict[str, list[Any]] = {}
    for row in tracked:
        by_article.setdefault(row.article_id, []).append(row)

    matches: list[ContentMatch] = []
    seen_files: set[str] = set()
    for article_id, hit_scope, title, snippet in ordered:
        candidates = by_article.get(article_id) or []
        if hit_scope and len(candidates) > 1:
            # The same slug can exist in two scopes. Prefer the row that says
            # it came from the scope this hit came from; keep rows whose scope
            # predates the column rather than dropping them.
            narrowed = [c for c in candidates if c.scope in (hit_scope, None)]
            candidates = narrowed or candidates
        for row in candidates:
            if row.record.id in seen_files:
                continue
            seen_files.add(row.record.id)
            matches.append(
                ContentMatch(
                    file=unified_from_record(row.record),
                    article_id=article_id,
                    scope=hit_scope or (row.scope or ""),
                    title=title,
                    snippet=snippet,
                    verbatim=False,
                )
            )
            if len(matches) >= capped:
                break
        if len(matches) >= capped:
            break

    if not matches:
        return ContentSearchResult(matches=[], scopes=scopes, degraded=None)

    matches = await _mark_verbatim(matches, kb_list)
    degraded = DEGRADED_VERBATIM if any(m.verbatim for m in matches) else None
    return ContentSearchResult(matches=matches, scopes=scopes, degraded=degraded)


async def _mark_verbatim(
    matches: list[ContentMatch], kb_list: KbListFn | None
) -> list[ContentMatch]:
    """Stamp ``verbatim`` on matches whose article was stored uncompiled.

    Probes only the scopes that actually produced a match (usually one), and
    only after the join, so a query that returns nothing costs no extra
    subprocess.
    """
    tables: dict[str, dict[str, str]] = {}
    for scope in {m.scope for m in matches if m.scope}:
        tables[scope] = await _compiled_with_for_scope(scope, kb_list)
    if not any(tables.values()):
        return matches
    out: list[ContentMatch] = []
    for m in matches:
        compiled_with = tables.get(m.scope, {}).get(m.article_id)
        out.append(
            ContentMatch(
                file=m.file,
                article_id=m.article_id,
                scope=m.scope,
                title=m.title,
                snippet=m.snippet,
                verbatim=compiled_with == _FALLBACK_COMPILED_WITH,
            )
        )
    return out
