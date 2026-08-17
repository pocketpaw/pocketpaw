# knowledge_router.py — Workspace-level knowledge browser router.
# Updated: 2026-08-04 (review follow-up) — security + tracking fixes:
#   * GET /uploads is WORKSPACE-scoped only: rows with a pocket_id are
#     excluded (files-service precedent — pocket reads are ACL-gated on
#     their own surface, and listing pocket-private metadata workspace-wide
#     was a bleed).
#   * POST /reingest-upload REFUSES pocket-scoped uploads (403
#     knowledge.upload_pocket_scoped) — ingesting them into workspace KB
#     lifted pocket-private content across the pocket ACL boundary.
#   * FL-11b tracking now actually fires: kb-go's ingest receipt keys the
#     id as "article" (not "id"); both reingest routes extract via
#     knowledge.extract_ingest_article_id and return a top-level article_id.
#   * POST /reingest re-points tracking rows when the recompile lands under
#     a new slug (MongoFileStore.reassign_kb_article, contained).
#   * GET /articles/{id} distinguishes kb outage (500
#     knowledge.kb_unavailable) from a genuine miss (404) instead of
#     404ing existing articles during a kb timeout.
#   * has_article: primary signal is the FL-11b column; the filename
#     fallback only applies to untracked uploads created BEFORE the
#     matching article was compiled (a fresh same-named re-upload is
#     pending, not compiled).
# Updated: 2026-08-04 — Living-wiki API for the /knowledge frontend rebuild:
#   * GET /articles rows now carry wiki metadata (summary, word_count,
#     compiled_with, version, categories, concepts, compiled_at) — kb list
#     output enriched from the scope's wiki frontmatter, since `kb list --json`
#     doesn't emit categories/concepts/compiled_at. The kb subprocess now runs
#     off the event loop (asyncio.to_thread) — it used to block the handler.
#   * NEW GET /articles/{article_id}?scope= — full article via `kb show`
#     (+ content, backlinks, compiled_at, source_docs); orphan raw docs are
#     served as synthetic uncompiled articles; 404 on unknown id/scope.
#   * NEW GET /stats — per-scope `kb stats` rollup across the workspace scope
#     and every agent scope visible to the caller.
#   * NEW POST /reingest — re-run an article's linked raw doc through the
#     hardened KnowledgeService.ingest_text_to_scope funnel.
#   * NEW POST /reingest-upload — resolve an uploaded file, extract via the
#     configured extraction chain, funnel through ingest_text_to_scope.
#   * NEW GET /uploads — the workspace's uploaded files eligible for ingest,
#     with a cheap has_article marker (FL-11b tracking + source-filename match).
#   Scope-accepting routes bind the client scope through the kb router's
#   allowlist pattern (kb.service.validate_scope_override + log_denial).
# Created: 2026-04-19 (Cluster C / PR1) — Adds GET /api/v1/knowledge/articles,
# a workspace-level rollup that unions the workspace KB with every per-agent
# KB inside the workspace. See docs/plans/FEATURE-HARDENING-PLAN.md §Cluster C
# and docs/plans/cluster-C-reality.md for the Wave 1 reality brief.
"""Workspace-level knowledge browser — FastAPI router.

Mounts at ``/api/v1/knowledge/*``. Kept separate from the per-workspace-scope
``/api/v1/kb/*`` router because the aggregation semantics differ: this view
fans out across every agent in the workspace, whereas ``/kb/articles`` is a
single-scope list.

Auth model:
    - ``kb.read`` action required on the active workspace for reads;
      ``kb.write`` for the reingest routes.
    - ``workspace_id`` query param, if provided, must match the caller's
      active workspace. We intentionally do not allow cross-workspace reads
      from this endpoint — the guard ``require_action_any_workspace('kb.read')``
      already pins the caller to their active workspace, and honouring a
      different ``workspace_id`` would leak KB across tenants.
    - Routes that accept a client ``scope`` bind it to the caller via
      ``kb.service.validate_scope_override`` (the same allowlist the /kb
      router uses), audit-logging denials.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.errors import CloudError, Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.agents.knowledge import (
    KnowledgeService,
    _kb,
    extract_ingest_article_id,
)
from pocketpaw_ee.cloud.kb import service as kb_service
from pocketpaw_ee.cloud.kb.dto import ReingestRequest, ReingestUploadRequest
from pocketpaw_ee.cloud.kb.workspace_aggregator import aggregate_workspace_articles
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.guards.audit import log_denial

logger = logging.getLogger(__name__)

# Regex matching kb-go's sanitize() — replaces any char outside [a-zA-Z0-9_-] with '_'.
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")
KB_HOME = os.path.expanduser("~/.knowledge-base")

router = APIRouter(
    prefix="/knowledge",
    tags=["Knowledge"],
    dependencies=[Depends(require_license)],
)


async def _list_workspace_agent_ids(workspace_id: str) -> list[str]:
    """Return every agent id that belongs to *workspace_id*. Routes
    through ``agents.service`` so this router stays out of
    ``ee.cloud.models.agent``."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    agents = await agents_service.list_agents(workspace_id)
    return [a.id for a in agents]


def _sanitize_scope(scope: str) -> str:
    """Mirror kb-go's sanitize() — replaces non-[a-zA-Z0-9_-] chars with '_'.

    kb-go stores articles under ``~/.knowledge-base/{sanitized_scope}/`` so the
    colon in ``workspace:abc`` becomes ``workspace_abc`` on disk."""
    return _SANITIZE_RE.sub("_", scope)


async def _resolve_scope(
    workspace_id: str,
    user_id: str,
    override: str | None,
    *,
    action: str,
) -> str:
    """Bind a client-supplied ``scope`` to the caller's allowlist.

    Same pattern as the /kb router: delegates to
    ``kb.service.validate_scope_override`` (own workspace + visible pockets +
    workspace agents + the caller's OWN ``user:``), audit-logs denials at
    ALERT, and re-raises ``Forbidden`` for ``_core.http`` to map to 403 JSON.
    """
    try:
        return await kb_service.validate_scope_override(workspace_id, user_id, override)
    except Forbidden:
        log_denial(
            actor=user_id,
            action=action,
            code="kb.scope_forbidden",
            resource_id=override or "",
            workspace_id=workspace_id,
            detail="KB scope override not bound to caller",
        )
        raise


def _contained_article_id(article_id: str) -> None:
    """Reject ids that could escape the scope dir on our direct file reads.

    Mirror of kb-go's ``containedID`` (issue #23): every id kb-go mints is
    slug-like, so a path separator or ``..`` is always hostile. Raising
    ``NotFound`` (not 400) avoids acting as a traversal oracle.
    """
    if not article_id or "/" in article_id or "\\" in article_id or ".." in article_id:
        raise NotFound("article", article_id)


def _read_frontmatter_file(md_path: str) -> dict[str, Any] | None:
    """Parse the JSON frontmatter block of one wiki ``.md`` file, or ``None``."""
    try:
        with open(md_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = json.loads(parts[1])
    except json.JSONDecodeError:
        return None
    return fm if isinstance(fm, dict) else None


def _read_scope_frontmatter(scope: str) -> dict[str, dict[str, Any]]:
    """Frontmatter for every wiki article in *scope*, keyed by article id.

    ``kb list --json`` doesn't emit categories/concepts/compiled_at, so the
    articles listing enriches its rows from the frontmatter kb-go itself
    writes (the same on-disk layout ``_list_orphan_raw_docs`` already reads).
    """
    wiki_dir = os.path.join(KB_HOME, _sanitize_scope(scope), "wiki")
    out: dict[str, dict[str, Any]] = {}
    for md_path in glob.glob(os.path.join(wiki_dir, "*.md")):
        fm = _read_frontmatter_file(md_path)
        if fm is not None:
            out[os.path.splitext(os.path.basename(md_path))[0]] = fm
    return out


def _load_raw_doc(scope: str, raw_id: str) -> dict[str, Any] | None:
    """Read one raw doc JSON (``raw/{id}.json``) for *scope*, or ``None``."""
    _contained_article_id(raw_id)
    raw_path = os.path.join(KB_HOME, _sanitize_scope(scope), "raw", f"{raw_id}.json")
    try:
        with open(raw_path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _list_orphan_raw_docs(scope: str) -> list[dict[str, Any]]:
    """Read raw docs that have *no* corresponding wiki article for *scope*.

    When kb-go ingests text it saves a raw doc first, then compiles a wiki
    article.  If compilation fails (LLM timeout, subprocess killed, etc.) the
    raw doc stays on disk without a wiki article — it becomes invisible to the
    normal ``kb list`` path.  This helper catches those orphans so the
    workspace knowledge browser still surfaces every piece of ingested content.
    """
    sanitized = _sanitize_scope(scope)
    scope_dir = os.path.join(KB_HOME, sanitized)
    wiki_dir = os.path.join(scope_dir, "wiki")
    raw_dir = os.path.join(scope_dir, "raw")

    if not os.path.isdir(raw_dir):
        return []

    # Collect the set of raw-doc IDs that ARE referenced by wiki articles
    # (via the ``source_docs`` frontmatter field).
    referenced_raws: set[str] = set()
    if os.path.isdir(wiki_dir):
        for md_path in glob.glob(os.path.join(wiki_dir, "*.md")):
            fm = _read_frontmatter_file(md_path)
            if fm is None:
                logger.debug("Failed to read wiki article %s", md_path)
                continue
            for sd in fm.get("source_docs", []) or []:
                referenced_raws.add(str(sd))

    orphans: list[dict[str, Any]] = []
    for raw_path in glob.glob(os.path.join(raw_dir, "*.json")):
        raw_id = os.path.splitext(os.path.basename(raw_path))[0]
        if raw_id in referenced_raws:
            continue  # Already surfaced through a wiki article.
        try:
            with open(raw_path, encoding="utf-8", errors="replace") as fh:
                doc = json.load(fh)
        except Exception:
            logger.debug("Failed to read raw doc %s", raw_path, exc_info=True)
            continue

        word_count = doc.get("word_count")
        orphans.append(
            {
                "id": raw_id,
                "title": doc.get("source") or doc.get("filename") or raw_id,
                "source": doc.get("source") or "",
                "updated_at": doc.get("ingested_at"),
                "word_count": word_count if isinstance(word_count, int) else 0,
            }
        )

    return orphans


def _call_kb_list(scope: str) -> list[Any]:
    """Wrap the kb-go ``list`` command. Non-list returns are coerced to ``[]``
    so the aggregator never sees surprising shapes.

    Enriches each kb row with the wiki-frontmatter fields ``kb list --json``
    doesn't emit (categories, concepts, compiled_at) so the living-wiki UI
    gets full rows without one ``kb show`` per article.

    Also folds in *orphan* raw docs — ingested files whose wiki compilation
    never completed — so the workspace knowledge browser surfaces every piece
    of content, not just fully-compiled articles."""
    wiki_articles: list[Any] = []
    try:
        result = _kb("list", "--scope", scope)
        if isinstance(result, list):
            wiki_articles = result
    except Exception as exc:  # noqa: BLE001
        logger.debug("kb list raised for scope=%s: %s", scope, exc)

    try:
        frontmatter = _read_scope_frontmatter(scope)
    except Exception:
        logger.debug("frontmatter scan failed for scope=%s", scope, exc_info=True)
        frontmatter = {}
    for row in wiki_articles:
        if not isinstance(row, dict):
            continue
        fm = frontmatter.get(str(row.get("id")), {})
        row.setdefault("categories", fm.get("categories") or [])
        row.setdefault("concepts", fm.get("concepts") or [])
        row.setdefault("compiled_at", fm.get("compiled_at"))

    # Append orphan raw docs as synthetic articles so they show up in the UI.
    try:
        orphan_rows = _list_orphan_raw_docs(scope)
    except Exception:
        logger.debug("orphan-raw-doc scan failed for scope=%s", scope, exc_info=True)
        orphan_rows = []

    return wiki_articles + orphan_rows


@router.get(
    "/articles",
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def list_workspace_articles(
    workspace_id_q: str | None = Query(None, alias="workspace_id"),
    agent_id: str | None = Query(
        None, description="Filter by agent; 'workspace' for workspace-only"
    ),
    limit: int | None = Query(
        None,
        ge=1,
        le=500,
        description="Max rows to return. Omit for the legacy one-shot "
        "full listing (backward compatible).",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Offset into the newest-first listing (with `limit`).",
    ),
    active_workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """List KB articles across the workspace + every agent in the workspace.

    Query params:
    -  ``workspace_id`` — optional; must match the caller's active workspace
       if set (prevents accidental cross-tenant reads).
    - ``agent_id`` — optional filter. ``"workspace"`` means workspace-only,
      otherwise restricts to one agent's KB.
    - ``limit`` / ``offset`` — optional offset pagination for the unified
      Files panel. The merge itself still fans out across every scope; only
      the response window is sliced. When ``limit`` is omitted the endpoint
      keeps returning every article (existing consumers unchanged).
    """
    if workspace_id_q is not None and workspace_id_q != active_workspace_id:
        raise Forbidden(
            "knowledge.workspace_mismatch",
            "workspace_id must match the caller's active workspace",
        )

    agent_ids = await _list_workspace_agent_ids(active_workspace_id)
    if agent_id is not None and agent_id != "workspace" and agent_id not in agent_ids:
        # Unknown agent or an agent outside this workspace — surface as empty
        # rather than leaking existence of agents in other workspaces.
        return {
            "articles": [],
            "total": 0,
            "has_more": False,
            "offset": offset,
            "limit": limit,
            "agent_ids": agent_ids,
        }

    articles = await aggregate_workspace_articles(
        workspace_id=active_workspace_id,
        agent_ids=agent_ids,
        # Off the event loop: _call_kb_list shells out to the kb binary per
        # scope; the aggregator awaits each returned coroutine.
        kb_list=lambda scope: asyncio.to_thread(_call_kb_list, scope),
        agent_filter=agent_id,
    )

    total = len(articles)
    page = articles[offset : offset + limit] if limit is not None else articles
    has_more = offset + len(page) < total

    return {
        "articles": [a.to_dict() for a in page],
        "total": total,
        "has_more": has_more,
        "offset": offset,
        "limit": limit,
        "agent_ids": agent_ids,
    }


# ---------------------------------------------------------------------------
# Single article — full body for the wiki reader
# ---------------------------------------------------------------------------


@router.get(
    "/articles/{article_id}",
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def get_workspace_article(
    article_id: str,
    scope: str | None = Query(None, description="kb scope; defaults to the active workspace"),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Full article for the wiki reader: list-row fields + content + backlinks.

    ``scope`` is bound to the caller via the kb allowlist. Unknown id or an
    id outside the scope → 404. An *orphan* raw doc (ingested but never
    compiled — the synthetic rows the listing surfaces) is served as an
    uncompiled article with ``orphan: true`` and the raw text as content.
    """
    resolved = await _resolve_scope(workspace_id, user_id, scope, action="kb.read")
    _contained_article_id(article_id)
    try:
        result = await asyncio.to_thread(_kb, "show", article_id, "--scope", resolved)
    except RuntimeError as exc:
        # A genuine miss surfaces as kb-go's `fatal("Article not found: ...")`
        # on stderr (exit 1) — fall through to the orphan raw-doc lookup.
        # Anything else (timeout, missing binary, transient failure) is an
        # OUTAGE: answering 404 there would tell the UI an existing article
        # vanished. Distinguish and 500 instead. Match the full "article not
        # found" phrase — the missing-BINARY error also says "not found".
        if "article not found" not in str(exc).lower():
            logger.warning("kb show failed for article=%s scope=%s: %s", article_id, resolved, exc)
            raise CloudError(500, "knowledge.kb_unavailable", str(exc)) from exc
        result = None
    if not isinstance(result, dict):
        raw = await asyncio.to_thread(_load_raw_doc, resolved, article_id)
        if raw is None:
            raise NotFound("article", article_id)
        word_count = raw.get("word_count")
        return {
            "id": article_id,
            "title": raw.get("source") or raw.get("filename") or article_id,
            "summary": "",
            "content": raw.get("raw_text") or "",
            "concepts": [],
            "categories": [],
            "backlinks": [],
            "word_count": word_count if isinstance(word_count, int) else 0,
            "compiled_with": None,
            "version": None,
            "compiled_at": None,
            "source_docs": [article_id],
            "scope": resolved,
            "orphan": True,
        }

    # `kb show --json` omits compiled_at and source_docs — read them from the
    # article's own frontmatter (kb-go's on-disk format).
    fm = (
        await asyncio.to_thread(
            _read_frontmatter_file,
            os.path.join(KB_HOME, _sanitize_scope(resolved), "wiki", f"{article_id}.md"),
        )
        or {}
    )
    result.setdefault("backlinks", [])
    result["compiled_at"] = fm.get("compiled_at")
    result["source_docs"] = [str(sd) for sd in fm.get("source_docs") or []]
    result["scope"] = resolved
    result["orphan"] = False
    return result


# ---------------------------------------------------------------------------
# Stats — per-scope rollup for the wiki header
# ---------------------------------------------------------------------------


@router.get(
    "/stats",
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def workspace_knowledge_stats(
    active_workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """`kb stats` per scope visible to the caller (workspace + its agents).

    Mirrors the articles aggregator's visibility rules and resilience: a
    scope whose stats call fails is skipped with a warning, never a 500.
    """
    agent_ids = await _list_workspace_agent_ids(active_workspace_id)
    scopes: list[tuple[str, str | None]] = [(f"workspace:{active_workspace_id}", None)]
    scopes += [(f"agent:{aid}", aid) for aid in agent_ids]

    rows: list[dict[str, Any]] = []
    for scope, agent_id in scopes:
        try:
            stats = await asyncio.to_thread(_kb, "stats", "--scope", scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb stats failed for scope=%s: %s", scope, exc)
            continue
        if not isinstance(stats, dict):
            continue
        rows.append(
            {
                "scope": scope,
                "agent_id": agent_id,
                "articles": stats.get("articles", 0),
                "words": stats.get("words", 0),
                "raw_docs": stats.get("raw_docs", 0),
                "concepts": stats.get("concepts", 0),
                "categories": stats.get("categories", 0),
            }
        )
    return {"stats": rows, "agent_ids": agent_ids}


# ---------------------------------------------------------------------------
# Reingest — re-run a raw doc through the hardened ingest funnel
# ---------------------------------------------------------------------------


@router.post(
    "/reingest",
    dependencies=[Depends(require_action_any_workspace("kb.write"))],
)
async def reingest_article(
    body: ReingestRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Re-run an article's linked raw doc through the ingest funnel.

    Resolution order for the raw text:
      1. ``article_id`` is a compiled article → its frontmatter's first
         ``source_docs`` entry names the raw doc.
      2. ``article_id`` IS a raw-doc id (the orphan rows the listing
         surfaces) → read ``raw/{article_id}.json`` directly.

    The ingest itself goes through
    :meth:`KnowledgeService.ingest_text_to_scope` — agent-backend compile on
    keyless boxes, verbatim-fallback rejection, subprocess off the loop.
    """
    resolved = await _resolve_scope(workspace_id, user_id, body.scope, action="kb.write")
    _contained_article_id(body.article_id)

    raw_doc_id = body.article_id
    fm = await asyncio.to_thread(
        _read_frontmatter_file,
        os.path.join(KB_HOME, _sanitize_scope(resolved), "wiki", f"{body.article_id}.md"),
    )
    if fm is not None:
        source_docs = [str(sd) for sd in fm.get("source_docs") or []]
        if not source_docs:
            raise NotFound("raw_doc", body.article_id)
        raw_doc_id = source_docs[0]

    raw = await asyncio.to_thread(_load_raw_doc, resolved, raw_doc_id)
    if raw is None:
        raise NotFound("article", body.article_id)
    text = str(raw.get("raw_text") or "")
    if not text.strip():
        raise ValidationError(
            "knowledge.empty_raw_doc",
            f"raw doc '{raw_doc_id}' has no text to reingest",
        )
    source = str(raw.get("source") or raw.get("filename") or raw_doc_id)

    try:
        result = await KnowledgeService.ingest_text_to_scope(resolved, text, source)
    except CloudError:
        raise
    except Exception as exc:
        logger.error("KB reingest failed (scope=%s): %s", resolved, exc, exc_info=True)
        raise CloudError(500, "knowledge.reingest_failed", str(exc)) from exc

    # kb-go's receipt keys the id as "article" — extract via the shared
    # helper, never result["id"] (that read is always None on real receipts).
    new_article_id = extract_ingest_article_id(result)

    # The recompile can land under a NEW slug; any upload row still tracking
    # the old id would purge a dead article on a later hide-from-AI toggle
    # while the live copy survives. Re-point the tracking. Contained — a
    # tracking failure never undoes the ingest that succeeded.
    if new_article_id and new_article_id != body.article_id:
        try:
            from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

            await MongoFileStore().reassign_kb_article(
                workspace_id,
                old_article_id=body.article_id,
                new_article_id=new_article_id,
                scope=resolved,
            )
        except Exception:
            logger.exception(
                "kb-article tracking reassign failed (old=%s new=%s)",
                body.article_id,
                new_article_id,
            )

    # no-event: KB state lives in kb-go's own store, outside the cloud
    # entity/event system; search-index refresh happens inside kb ingest and
    # no bus consumer subscribes to article changes today.
    return {
        "scope": resolved,
        "article_id": body.article_id,
        "new_article_id": new_article_id,
        "raw_doc_id": raw_doc_id,
        "source": source,
        "result": result,
    }


@router.post(
    "/reingest-upload",
    dependencies=[Depends(require_action_any_workspace("kb.write"))],
)
async def reingest_upload(
    body: ReingestUploadRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Extract one uploaded file and ingest it through the funnel.

    Mirrors the FileReady listener's pipeline (resolve → extract → funnel)
    but synchronously, returning the ingest result — the client drives the
    loop one upload per call. Hidden files (``hide_from_ai``) are refused.
    """
    resolved = await _resolve_scope(workspace_id, user_id, body.scope, action="kb.write")

    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    store = MongoFileStore()
    doc = await store.get_doc_scoped(body.upload_id, workspace_id)
    if doc is None:
        raise NotFound("upload", body.upload_id)
    if getattr(doc, "hide_from_ai", False):
        raise Forbidden(
            "knowledge.upload_hidden",
            "this file is hidden from AI and cannot be ingested",
        )
    # Pocket-scoped uploads are ACL-gated per pocket (has_edit_access); this
    # v1 workspace surface has no pocket ACL check, and ingesting a pocket
    # file into workspace KB would lift pocket-private content across that
    # boundary. Refuse — pocket content reingests on the pocket surface.
    if getattr(doc, "pocket_id", None):
        raise Forbidden(
            "knowledge.upload_pocket_scoped",
            "this file belongs to a pocket; reingest it from the pocket surface",
        )

    adapter = _resolve_upload_adapter()
    if adapter is None:
        raise CloudError(500, "knowledge.upload_adapter_unavailable", "upload storage not mounted")

    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.extraction import build_chain
    from pocketpaw_ee.cloud.uploads.resolver import materialize_to_local_path

    mime = doc.mime or "application/octet-stream"
    async with materialize_to_local_path(
        adapter, doc.storage_key, mime=mime, filename=doc.filename
    ) as path:
        if path is None:
            raise CloudError(
                500, "knowledge.upload_unreadable", f"could not read upload '{body.upload_id}'"
            )
        try:
            extraction = await build_chain(get_settings()).run(path, mime)
        except Exception as exc:
            logger.error("extraction failed for upload=%s: %s", body.upload_id, exc, exc_info=True)
            raise CloudError(500, "knowledge.extraction_failed", str(exc)) from exc

    text = (extraction.text or "").strip()
    if not text:
        raise ValidationError(
            "knowledge.extraction_empty",
            f"no text could be extracted from '{doc.filename}'",
        )

    try:
        result = await KnowledgeService.ingest_text_to_scope(resolved, text, doc.filename)
    except CloudError:
        raise
    except Exception as exc:
        logger.error(
            "KB reingest-upload failed (scope=%s, upload=%s): %s",
            resolved,
            body.upload_id,
            exc,
            exc_info=True,
        )
        raise CloudError(500, "knowledge.reingest_failed", str(exc)) from exc

    # FL-11b tracking so a later hide-from-AI toggle can purge the article.
    # Contained — a tracking failure never undoes the ingest. The id comes
    # from the shared receipt helper: kb-go keys it as "article", not "id".
    article_id = extract_ingest_article_id(result)
    if article_id:
        try:
            # no-event: FL-11b tracking is a bookkeeping column consumed only
            # by the hide-from-AI purge read path; no bus consumer exists.
            await store.set_kb_article(
                body.upload_id, workspace_id, article_id=article_id, scope=resolved
            )
        except Exception:
            logger.exception("kb-article tracking failed for upload=%s", body.upload_id)

    # no-event: KB state lives in kb-go's own store, outside the cloud
    # entity/event system; search-index refresh happens inside kb ingest and
    # no bus consumer subscribes to article changes today.
    return {
        "scope": resolved,
        "upload_id": body.upload_id,
        "filename": doc.filename,
        "article_id": article_id,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Uploads — the workspace's files eligible for ingest
# ---------------------------------------------------------------------------


def _resolve_upload_adapter():
    """The EE upload singleton's storage adapter, or ``None`` when unmounted.

    Same lazy-import pattern as the FileReady listener so test harnesses can
    monkeypatch the router singleton.
    """
    try:
        from pocketpaw_ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER
    except Exception:
        logger.exception("upload adapter import failed")
        return None


def _sources_with_articles(scope: str) -> dict[str, str | None]:
    """Source filename → latest ``compiled_at`` of *scope*'s compiled articles.

    Fallback signal for ``has_article`` on uploads that predate the FL-11b
    ``kb_article_id`` tracking column. The compiled_at value lets the caller
    reject false positives: a FRESH re-upload of a same-named file matches
    the filename but was created AFTER the article was compiled, so it is
    pending, not compiled.
    """
    sanitized = _sanitize_scope(scope)
    # raw-doc id → compiled_at of the (latest) article referencing it.
    referenced: dict[str, str | None] = {}
    for fm in _read_scope_frontmatter(scope).values():
        compiled_at = fm.get("compiled_at")
        compiled_at = str(compiled_at) if compiled_at else None
        for sd in fm.get("source_docs") or []:
            raw_id = str(sd)
            existing = referenced.get(raw_id)
            if existing is None or (compiled_at is not None and compiled_at > existing):
                referenced[raw_id] = compiled_at
    sources: dict[str, str | None] = {}
    for raw_id, compiled_at in referenced.items():
        raw_path = os.path.join(KB_HOME, sanitized, "raw", f"{raw_id}.json")
        try:
            with open(raw_path, encoding="utf-8", errors="replace") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("source"):
            source = str(doc["source"])
            existing = sources.get(source)
            if source not in sources or (
                compiled_at is not None and (existing is None or compiled_at > existing)
            ):
                sources[source] = compiled_at
    return sources


def _parse_compiled_at(value: str | None) -> datetime | None:
    """Parse kb-go's RFC3339 ``compiled_at`` to an aware datetime, or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """Naive datetimes from the Mongo store are UTC by convention — tag them."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.get(
    "/uploads",
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def list_ingestable_uploads(
    scope: str | None = Query(None, description="kb scope; defaults to the active workspace"),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """The WORKSPACE's uploaded files eligible for KB ingest.

    Workspace-scoped only: rows with a ``pocket_id`` are excluded (files-
    service precedent — pocket files are ACL-gated on the pocket surface,
    and listing their metadata workspace-wide would bleed pocket privacy).
    Files hidden from AI are excluded too — they are not eligible.

    ``has_article`` is derived cheaply, no new tracking: primarily the
    FL-11b ``kb_article_id`` column (stamped by the FileReady listener and
    the reingest-upload route) matched against the resolved scope. For
    untracked rows only, a filename-vs-article-sources fallback covers
    uploads indexed before the tracking column existed — guarded by
    compiled_at so a FRESH re-upload of a same-named file (created after
    the article was compiled) reads as pending, not compiled.
    """
    resolved = await _resolve_scope(workspace_id, user_id, scope, action="kb.read")

    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    try:
        known_sources = await asyncio.to_thread(_sources_with_articles, resolved)
    except Exception:
        logger.debug("source scan failed for scope=%s", resolved, exc_info=True)
        known_sources = {}

    uploads: list[dict[str, Any]] = []
    async for row in MongoFileStore().iter_by_workspace(workspace_id):
        if row.get("hide_from_ai"):
            continue
        if row.get("pocket_id"):
            continue  # pocket-private — not listable on the workspace surface
        created_at = row.get("created_at")
        kb_article_id = row.get("kb_article_id")
        kb_scope = row.get("kb_scope")
        if kb_article_id:
            has_article = bool(kb_scope is None or kb_scope == resolved)
        else:
            # Legacy fallback: only trust the filename match when THIS upload
            # predates the article's compile — otherwise a new same-named
            # upload would instantly read as compiled and drop out of the
            # rebuild/poll candidates.
            filename = row.get("filename")
            compiled_at = _parse_compiled_at(
                known_sources.get(filename) if filename in known_sources else None
            )
            created_cmp = _as_aware_utc(created_at)
            has_article = bool(
                compiled_at is not None and created_cmp is not None and created_cmp <= compiled_at
            )
        uploads.append(
            {
                "id": row.get("file_id"),
                "filename": row.get("filename"),
                "mime": row.get("mime"),
                "size": row.get("size"),
                "uploaded_at": created_at.isoformat() if created_at else None,
                "has_article": has_article,
            }
        )
    return {"uploads": uploads, "total": len(uploads), "scope": resolved}
