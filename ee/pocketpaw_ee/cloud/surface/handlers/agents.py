# agents.py — /agents surface preamble.
#
# Created: 2026-05-24 — Workspace agents list. Reads via
# ``agents_service.list_agents`` (tenancy via workspace_id).
#
# Changes: 2026-08-03 (feat/prompt-entity-suffix) — renders through
# ``unaddressed_line("agent", ...)``. This handler was ALREADY the one that got
# it right: it carried ``slug``, the only surface row in the package with any
# identifier at all. It is converted anyway, because the contract test checks the
# SHAPE of the call — a hand-rolled row that happens to be correct today is one
# edit away from not being, and the exemplar is the worst place to leave that.
#
# NO TOOL TAKES AN AGENT ID — enumerating every MCP server's schema found no
# ``agent_id`` parameter, required or optional — so no id is rendered. The
# ``slug`` stays, because it is what a human and the agent both use to refer to
# one, and it was already there. The ``"agent"`` literal is checked against the
# derived kind set; see files.py for why that beats an allow-list entry.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. Mutable state, read as a LIST (every agent's name and
# slug), so the key is a digest of what was rendered rather than a revision:
# it moves when an agent is added, removed or renamed and holds still
# otherwise. The unavailable branch reads nothing and gets its own exact key.

from __future__ import annotations

import logging

from pocketpaw.prompt.entity import unaddressed_line
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
            rows.append(
                unaddressed_line(
                    "agent",
                    getattr(a, "name", None),
                    slug=getattr(a, "slug", None),
                )
            )
        if len(agents) > LIST_LIMIT:
            rows.append(f"... (+{len(agents) - LIST_LIMIT} more)")
        parts.append("<agents-list>\n" + "\n".join(rows) + "\n</agents-list>")
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("agents", text))


__all__ = ["build_preamble"]
