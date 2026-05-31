# pockets_list.py — /pockets index preamble.
#
# Created: 2026-05-24 — Summarises the user's pocket list with counts
# and a top-N listing so the agent can answer "what pockets do I
# have?" without an extra round-trip. Uses ``pockets_service.list_pockets``
# (tenancy enforced).

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the pockets-list surface preamble."""
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pockets = await pockets_service.list_pockets(workspace_id, user_id)
    except Exception:
        logger.debug("pockets_list_handler: list_pockets failed", exc_info=True)
        return (
            '<surface kind="pockets" route="/pockets" />'
            "<pockets-snapshot>(unavailable)</pockets-snapshot>"
        )

    total = len(pockets)
    parts = [
        '<surface kind="pockets" route="/pockets" />',
        f'<pockets-snapshot count="{total}" />',
    ]
    if total == 0:
        parts.append("<pockets-list>(no pockets yet)</pockets-list>")
    else:
        rows = []
        for p in pockets[:LIST_LIMIT]:
            name = p.get("name") or "(unnamed)"
            kind = p.get("type") or "custom"
            widget_count = len(p.get("widgets", []) or [])
            agent_count = len(p.get("agents", []) or [])
            rows.append(f"- {name} (type={kind}, widgets={widget_count}, agents={agent_count})")
        if total > LIST_LIMIT:
            rows.append(f"... (+{total - LIST_LIMIT} more)")
        parts.append("<pockets-list>\n" + "\n".join(rows) + "\n</pockets-list>")
    return truncate_preamble("\n".join(parts))


__all__ = ["build_preamble"]
