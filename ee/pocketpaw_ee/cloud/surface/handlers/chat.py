# chat.py — /chat surface preamble.
#
# Updated: 2026-05-24 — The sessions service now exposes a real
# ``list_for_user`` helper, so the forward-compat getattr probe in
# ``_session_count`` can go. Direct call, graceful try/except remains
# for genuine runtime failures (DB down, etc.).
#
# Originally created: 2026-05-24 — When the user is on the chat surface
# itself (not a pocket / not the home dashboard) we keep the preamble
# minimal: the agent already has the conversation state on hand. We
# surface the session count as a tiny hint so the agent can answer
# "how many threads do I have?" without an extra round-trip.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the chat-surface preamble."""
    count = await _session_count(workspace_id, user_id)
    parts = ['<surface kind="chat" route="/chat" />']
    if count is None:
        parts.append("<chat-snapshot>(session count unavailable)</chat-snapshot>")
    else:
        parts.append(f'<chat-snapshot sessions="{count}" />')
    return truncate_preamble("\n".join(parts))


async def _session_count(workspace_id: str, user_id: str) -> int | None:
    """Best-effort session count. Returns ``None`` on any failure."""
    try:
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        sessions = await sessions_service.list_for_user(workspace_id=workspace_id, user_id=user_id)
        return len(sessions or [])
    except Exception:
        logger.debug("chat_handler: session count failed", exc_info=True)
        return None


__all__ = ["build_preamble"]
