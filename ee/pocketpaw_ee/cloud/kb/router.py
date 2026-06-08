# router.py — Knowledge base domain router for ee/cloud.
# Updated: 2026-06-08 (VIP Onboarding Phase B) — bound the client ``scope``
# override to the caller. The four override-accepting endpoints (search,
# ingest/text, ingest/url, lint) now resolve ``body.scope`` through
# ``kb.service.validate_scope_override``, an allowlist (own workspace + visible
# pockets + workspace agents + the caller's OWN user:) that rejects anything
# else with Forbidden("kb.scope_forbidden"). This closes the cross-member leak
# where any authenticated member could read or poison another member's private
# ``user:{victim}`` KB via the REST door — the same boundary the chat-path gate
# enforces. Denials are audit-logged at ALERT via ``log_denial``.
# Updated: 2026-04-07 — Switched from Python knowledge_base package to kb Go binary.
# All operations delegate to the kb binary via subprocess. Same REST API surface.
"""Knowledge base domain — FastAPI router.

Workspace-scoped knowledge base endpoints consumed by the wiki pocket template
and other KB-aware UI components. Delegates to the kb Go binary.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.agents.knowledge import _extract_url, _kb
from pocketpaw_ee.cloud.kb import service as kb_service
from pocketpaw_ee.cloud.kb.dto import (
    IngestTextRequest,
    IngestUrlRequest,
    LintRequest,
    SearchRequest,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.shared.errors import CloudError, Forbidden, NotFound
from pocketpaw_ee.guards.audit import log_denial

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["Knowledge Base"], dependencies=[Depends(require_license)])


def _scope(workspace_id: str, override: str | None = None) -> str:
    """Scope for the GET endpoints that accept NO client override.

    These read the caller's active workspace only (``override`` is never
    threaded from the wire), so the permissive resolve is safe here. The
    override-accepting POST endpoints use ``_resolve_scope`` instead, which
    binds the override to the caller via the allowlist validator.
    """
    return override or f"workspace:{workspace_id}"


async def _resolve_scope(
    workspace_id: str,
    user_id: str,
    override: str | None,
    *,
    action: str,
) -> str:
    """Validate a client-supplied ``scope`` override against the caller's allowlist.

    Delegates to ``kb.service.validate_scope_override`` (own workspace + visible
    pockets + workspace agents + the caller's OWN ``user:``). On denial, audits
    the attempt at ALERT before re-raising so a probing member shows up in the
    RBAC denial log, then re-raises ``Forbidden`` for ``_core.http`` to map to a
    403 JSON body. Never raises ``HTTPException``.
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


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.post("/search", dependencies=[Depends(require_action_any_workspace("kb.read"))])
async def search_kb(
    body: SearchRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Search KB articles — returns metadata + snippet."""
    scope = await _resolve_scope(workspace_id, user_id, body.scope, action="kb.read")
    results = _kb("search", body.query, "--scope", scope, "--limit", str(body.limit))
    if not isinstance(results, list):
        results = []
    return {"results": results, "total": len(results)}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@router.post("/ingest/text", dependencies=[Depends(require_action_any_workspace("kb.write"))])
async def ingest_text(
    body: IngestTextRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Ingest plain text into the workspace knowledge base."""
    scope = await _resolve_scope(workspace_id, user_id, body.scope, action="kb.write")
    try:
        return _kb("ingest", "--scope", scope, "--source", body.source, input_text=body.text)
    except Exception as exc:
        logger.error("KB text ingest failed: %s", exc, exc_info=True)
        raise CloudError(500, "kb.ingest_failed", str(exc)) from exc


@router.post("/ingest/url", dependencies=[Depends(require_action_any_workspace("kb.write"))])
async def ingest_url(
    body: IngestUrlRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Fetch and ingest a URL into the workspace knowledge base."""
    scope = await _resolve_scope(workspace_id, user_id, body.scope, action="kb.write")
    try:
        text = await _extract_url(body.url)
        return _kb("ingest", "--scope", scope, "--source", body.url, input_text=text)
    except Exception as exc:
        logger.error("KB URL ingest failed: %s", exc, exc_info=True)
        raise CloudError(500, "kb.ingest_failed", str(exc)) from exc


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


@router.post("/lint", dependencies=[Depends(require_action_any_workspace("kb.read"))])
async def lint_kb(
    body: LintRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Run health checks on the knowledge base."""
    scope = await _resolve_scope(workspace_id, user_id, body.scope, action="kb.read")
    issues = _kb("lint", "--scope", scope)
    if not isinstance(issues, list):
        issues = []
    return {"issues": issues, "total": len(issues)}


# ---------------------------------------------------------------------------
# Browse — single article / concept
# ---------------------------------------------------------------------------


@router.get(
    "/article/{article_id}",
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def get_article(
    article_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Get a full article by ID (includes content)."""
    scope = _scope(workspace_id)
    try:
        result = _kb("show", article_id, "--scope", scope)
        if isinstance(result, dict):
            return result
        raise NotFound("article", article_id)
    except RuntimeError:
        raise NotFound("article", article_id)


@router.get(
    "/concept/{name}",
    dependencies=[Depends(require_action_any_workspace("kb.read"))],
)
async def get_concept_articles(
    name: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Get all articles associated with a concept."""
    scope = _scope(workspace_id)
    results = _kb("search", name, "--scope", scope, "--limit", "20")
    if not isinstance(results, list):
        results = []
    return {"concept": name, "articles": results, "total": len(results)}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats", dependencies=[Depends(require_action_any_workspace("kb.read"))])
async def kb_stats(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Get knowledge base statistics."""
    scope = _scope(workspace_id)
    return _kb("stats", "--scope", scope)


# ---------------------------------------------------------------------------
# List all — for first load
# ---------------------------------------------------------------------------


@router.get("/articles", dependencies=[Depends(require_action_any_workspace("kb.read"))])
async def list_articles(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """List all articles (metadata only)."""
    scope = _scope(workspace_id)
    articles = _kb("list", "--scope", scope)
    if not isinstance(articles, list):
        articles = []
    return {"articles": articles, "total": len(articles)}


@router.get("/concepts", dependencies=[Depends(require_action_any_workspace("kb.read"))])
async def list_concepts(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """List all concepts."""
    scope = _scope(workspace_id)
    stats = _kb("stats", "--scope", scope)
    return {"concepts": stats.get("concepts", 0) if isinstance(stats, dict) else 0}
