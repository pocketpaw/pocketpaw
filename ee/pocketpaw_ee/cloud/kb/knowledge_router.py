# knowledge_router.py — Workspace-level knowledge browser router.
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
    - ``kb.read`` action required on the active workspace.
    - ``workspace_id`` query param, if provided, must match the caller's
      active workspace. We intentionally do not allow cross-workspace reads
      from this endpoint — the guard ``require_action_any_workspace('kb.read')``
      already pins the caller to their active workspace, and honouring a
      different ``workspace_id`` would leak KB across tenants.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.kb.workspace_aggregator import aggregate_workspace_articles
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)

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
            try:
                with open(md_path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                if not text.startswith("---"):
                    continue
                parts = text.split("---", 2)
                if len(parts) < 3:
                    continue
                fm = json.loads(parts[1])
                for sd in fm.get("source_docs", []) or []:
                    referenced_raws.add(str(sd))
            except Exception:
                logger.debug("Failed to read wiki article %s", md_path, exc_info=True)

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

        orphans.append(
            {
                "id": raw_id,
                "title": doc.get("source") or doc.get("filename") or raw_id,
                "source": doc.get("source") or "",
                "updated_at": doc.get("ingested_at"),
            }
        )

    return orphans


def _call_kb_list(scope: str) -> list[Any]:
    """Wrap the kb-go ``list`` command. Non-list returns are coerced to ``[]``
    so the aggregator never sees surprising shapes.

    Also folds in *orphan* raw docs — ingested files whose wiki compilation
    never completed — so the workspace knowledge browser surfaces every piece
    of content, not just fully-compiled articles."""
    from pocketpaw_ee.cloud.agents.knowledge import _kb

    wiki_articles: list[Any] = []
    try:
        result = _kb("list", "--scope", scope)
        if isinstance(result, list):
            wiki_articles = result
    except Exception as exc:  # noqa: BLE001
        logger.debug("kb list raised for scope=%s: %s", scope, exc)

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
    active_workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """List KB articles across the workspace + every agent in the workspace.

    Query params:
    -  ``workspace_id`` — optional; must match the caller's active workspace
       if set (prevents accidental cross-tenant reads).
    - ``agent_id`` — optional filter. ``"workspace"`` means workspace-only,
      otherwise restricts to one agent's KB.
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
        return {"articles": [], "total": 0, "agent_ids": agent_ids}

    articles = await aggregate_workspace_articles(
        workspace_id=active_workspace_id,
        agent_ids=agent_ids,
        kb_list=_call_kb_list,
        agent_filter=agent_id,
    )

    return {
        "articles": [a.to_dict() for a in articles],
        "total": len(articles),
        "agent_ids": agent_ids,
    }
