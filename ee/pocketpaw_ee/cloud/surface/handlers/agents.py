# agents.py — /agents surface preamble.
#
# Created: 2026-05-24 — Workspace agents list. Reads via
# ``agents_service.list_agents`` (tenancy via workspace_id).

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the agents-list surface preamble."""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agents = await agents_service.list_agents(workspace_id)
    except Exception:
        logger.debug("agents_handler: list failed", exc_info=True)
        return (
            '<surface kind="agents" route="/agents" />'
            "<agents-snapshot>(unavailable)</agents-snapshot>"
        )

    parts = [
        '<surface kind="agents" route="/agents" />',
        f'<agents-snapshot count="{len(agents)}" />',
    ]
    if not agents:
        parts.append("<agents-list>(no agents in workspace)</agents-list>")
    else:
        rows = []
        for a in agents[:LIST_LIMIT]:
            name = getattr(a, "name", None) or "(unnamed)"
            slug = getattr(a, "slug", None) or "?"
            rows.append(f"- {name} (slug={slug})")
        if len(agents) > LIST_LIMIT:
            rows.append(f"... (+{len(agents) - LIST_LIMIT} more)")
        parts.append("<agents-list>\n" + "\n".join(rows) + "\n</agents-list>")
    return truncate_preamble("\n".join(parts))


__all__ = ["build_preamble"]
