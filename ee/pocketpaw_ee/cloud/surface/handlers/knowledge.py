# knowledge.py — /knowledge surface preamble.
#
# Created: 2026-05-24 — KB scope listing for the knowledge surface.
# Stays minimal — the kb stack varies per deploy; if the canonical
# helper isn't reachable we emit a placeholder so the agent still
# knows the user is on /knowledge.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the knowledge-surface preamble."""
    scopes = await _list_scopes(workspace_id)
    parts = ['<surface kind="knowledge" route="/knowledge" />']
    if not scopes:
        parts.append("<knowledge-snapshot>(no scopes detected)</knowledge-snapshot>")
    else:
        rows = [f"- {s}" for s in scopes[:10]]
        body = "\n".join(rows)
        parts.append(f'<knowledge-scopes count="{len(scopes)}">\n{body}\n</knowledge-scopes>')
    return truncate_preamble("\n".join(parts))


async def _list_scopes(workspace_id: str) -> list[str]:
    """Best-effort scope listing. Returns ``[]`` when no reachable backend.

    The kb stack is optional in some deploys — we'd rather emit a
    placeholder than crash. The canonical hook would be
    ``KnowledgeService.list_scopes(workspace_id)`` once that surface
    lands; until then a static workspace scope is the safest output.
    """
    if not workspace_id:
        return []
    return [f"workspace:{workspace_id}"]


__all__ = ["build_preamble"]
