# router.py — Knowledge base domain router for ee/cloud.
# Updated: 2026-04-07 — Switched from Python knowledge_base package to kb Go binary.
# All operations delegate to the kb binary via subprocess. Same REST API surface.
"""Knowledge base domain — FastAPI router.

Workspace-scoped knowledge base endpoints consumed by the wiki pocket template
and other KB-aware UI components. Delegates to the kb Go binary.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ee.cloud.agents.knowledge import _extract_url, _kb
from ee.cloud.kb.schemas import IngestTextRequest, IngestUrlRequest, LintRequest, SearchRequest
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.errors import CloudError, NotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["Knowledge Base"], dependencies=[Depends(require_license)])


def _scope(workspace_id: str, override: str | None = None) -> str:
    return override or f"workspace:{workspace_id}"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.post("/search")
async def search_kb(
    body: SearchRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Search KB articles — returns metadata + snippet."""
    scope = _scope(workspace_id, body.scope)
    results = _kb("search", body.query, "--scope", scope, "--limit", str(body.limit))
    if not isinstance(results, list):
        results = []
    return {"results": results, "total": len(results)}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@router.post("/ingest/text")
async def ingest_text(
    body: IngestTextRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Ingest plain text into the workspace knowledge base."""
    scope = _scope(workspace_id, body.scope)
    try:
        return _kb("ingest", "--scope", scope, "--source", body.source, input_text=body.text)
    except Exception as exc:
        logger.error("KB text ingest failed: %s", exc, exc_info=True)
        raise CloudError(500, "kb.ingest_failed", str(exc)) from exc


@router.post("/ingest/url")
async def ingest_url(
    body: IngestUrlRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Fetch and ingest a URL into the workspace knowledge base."""
    scope = _scope(workspace_id, body.scope)
    try:
        text = await _extract_url(body.url)
        return _kb("ingest", "--scope", scope, "--source", body.url, input_text=text)
    except Exception as exc:
        logger.error("KB URL ingest failed: %s", exc, exc_info=True)
        raise CloudError(500, "kb.ingest_failed", str(exc)) from exc


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


@router.post("/lint")
async def lint_kb(
    body: LintRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Run health checks on the knowledge base."""
    scope = _scope(workspace_id, body.scope)
    issues = _kb("lint", "--scope", scope)
    if not isinstance(issues, list):
        issues = []
    return {"issues": issues, "total": len(issues)}


# ---------------------------------------------------------------------------
# Browse — single article / concept
# ---------------------------------------------------------------------------


@router.get("/article/{article_id}")
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


@router.get("/concept/{name}")
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


@router.get("/stats")
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


@router.get("/articles")
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


@router.get("/concepts")
async def list_concepts(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """List all concepts."""
    scope = _scope(workspace_id)
    stats = _kb("stats", "--scope", scope)
    return {"concepts": stats.get("concepts", 0) if isinstance(stats, dict) else 0}
