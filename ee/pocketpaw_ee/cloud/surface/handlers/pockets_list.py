# pockets_list.py — /pockets index preamble.
#
# Created: 2026-05-24 — Summarises the user's pocket list with counts
# and a top-N listing so the agent can answer "what pockets do I
# have?" without an extra round-trip. Uses ``pockets_service.list_pockets``
# (tenancy enforced).
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. This handler DOES read mutable state (the workspace's
# pockets, with each one's name, type, widget count and agent count), but it
# reads a LIST — there is no single revision to key on, and pulling every
# pocket's ``updatedAt`` would cost more than the preamble. So the key is a
# digest of what was rendered: it moves when a pocket is created, deleted,
# renamed or gains a widget, and it holds still across two turns that render
# the same list. It is blind to a change past ``LIST_LIMIT``, which is
# survivable precisely because such a change is not in the prompt either.
# The unavailable branch reads nothing and gets its own exact key.

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
    """Render the pockets-list surface preamble."""
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pockets = await pockets_service.list_pockets(workspace_id, user_id)
    except Exception:
        logger.debug("pockets_list_handler: list_pockets failed", exc_info=True)
        return SurfacePreamble(
            text=(
                '<surface kind="pockets" route="/pockets" />'
                "<pockets-snapshot>(unavailable)</pockets-snapshot>"
            ),
            cache_key=meta_key("pockets", "unavailable"),
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
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("pockets", text))


__all__ = ["build_preamble"]
