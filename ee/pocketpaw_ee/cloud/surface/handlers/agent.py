# agent.py — /agents/[id] surface preamble.
#
# Created: 2026-05-24 — Detail view for one agent. Reads via
# ``agents_service.get``; falls back to a minimal preamble when the
# agent id is missing or the lookup fails.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the single-agent surface preamble."""
    if not meta.agent_id:
        return '<surface kind="agent" route="/agents/?" />'

    try:
        from pocketpaw_ee.cloud.agents import service as agents_service

        agent = await agents_service.get(meta.agent_id)
    except Exception:
        logger.debug("agent_handler: get(%s) failed", meta.agent_id, exc_info=True)
        return (
            f'<surface kind="agent" route="/agents/{meta.agent_id}" />'
            "<agent-snapshot>(unavailable)</agent-snapshot>"
        )

    name = getattr(agent, "name", None) or "(unnamed)"
    slug = getattr(agent, "slug", None) or "?"
    return truncate_preamble(
        f'<surface kind="agent" route="/agents/{meta.agent_id}" />\n'
        f'<current-agent id="{meta.agent_id}" name="{name}" slug="{slug}" />'
    )


__all__ = ["build_preamble"]
