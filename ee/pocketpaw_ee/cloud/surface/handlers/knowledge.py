# knowledge.py — /knowledge surface preamble.
#
# Created: 2026-05-24 — KB scope listing for the knowledge surface.
# Updated: 2026-05-24 — Wired through ``ee.cloud.kb.service.list_scopes``
# so the preamble renders the real workspace + pocket + agent scopes
# the kb-go store actually carries, rather than the
# ``[f"workspace:{workspace_id}"]`` synthetic fallback. The
# kb-unreachable path still degrades to "(no scopes detected)" so a
# missing kb binary doesn't break the chat send.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.kb import service as kb_service
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the knowledge-surface preamble."""
    scopes = await _list_scopes(workspace_id, user_id)
    parts = ['<surface kind="knowledge" route="/knowledge" />']
    if not scopes:
        parts.append("<knowledge-snapshot>(no scopes detected)</knowledge-snapshot>")
    else:
        rows = [f"- {s}" for s in scopes[:10]]
        body = "\n".join(rows)
        parts.append(f'<knowledge-scopes count="{len(scopes)}">\n{body}\n</knowledge-scopes>')
    return truncate_preamble("\n".join(parts))


async def _list_scopes(workspace_id: str, user_id: str) -> list[str]:
    """Resolve the workspace's real KB scopes via the canonical service.

    Failures are isolated — the kb stack is optional in some deploys and
    a probe outage must never crash the preamble. We log and fall back
    to ``[]`` so the handler renders ``(no scopes detected)`` instead.
    """
    if not workspace_id:
        return []
    try:
        return await kb_service.list_scopes(workspace_id, user_id)
    except Exception:
        logger.exception(
            "kb_service.list_scopes failed for workspace=%s; emitting empty list",
            workspace_id,
        )
        return []


__all__ = ["build_preamble"]
