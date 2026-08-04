# agents.py — /agents surface preamble.
#
# Created: 2026-05-24 — Workspace agents list. Reads via
# ``agents_service.list_agents`` (tenancy via workspace_id).
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. Mutable state, read as a LIST (every agent's name and
# slug), so the key is a digest of what was rendered rather than a revision:
# it moves when an agent is added, removed or renamed and holds still
# otherwise. The unavailable branch reads nothing and gets its own exact key.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import (
    content_key,
    meta_key,
    truncate_preamble,
)

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the agents-list surface preamble."""
    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agents = await agents_service.list_agents(workspace_id)
    except Exception:
        logger.debug("agents_handler: list failed", exc_info=True)
        return SurfacePreamble(
            text=(
                '<surface kind="agents" route="/agents" />'
                "<agents-snapshot>(unavailable)</agents-snapshot>"
            ),
            cache_key=meta_key("agents", "unavailable"),
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
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("agents", text))


__all__ = ["build_preamble"]
