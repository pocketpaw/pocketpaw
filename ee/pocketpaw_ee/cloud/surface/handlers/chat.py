# chat.py — /chat surface preamble.
#
# On the chat surface itself the agent already has the conversation on hand, so
# the preamble stays minimal: the surface tag plus the caller's chat-thread
# count (chat-surface and legacy null rows only, via
# ``sessions_service.count_for_user``, a server-side count), so the agent can
# answer "how many threads do I have?" without a round-trip. A failed count
# renders as "unavailable" rather than failing the turn.
#
# Returns a ``SurfacePreamble`` whose cache key is a digest of the rendered
# text. That key is exact: the count is the one mutable thing read and the one
# thing rendered, so it moves when a thread is created or deleted.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import content_key, truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the chat-surface preamble."""
    count = await _session_count(workspace_id, user_id)
    parts = ['<surface kind="chat" route="/chat" />']
    if count is None:
        parts.append("<chat-snapshot>(session count unavailable)</chat-snapshot>")
    else:
        parts.append(f'<chat-snapshot sessions="{count}" />')
    text = truncate_preamble("\n".join(parts))
    return SurfacePreamble(text=text, cache_key=content_key("chat", text))


async def _session_count(workspace_id: str, user_id: str) -> int | None:
    """Best-effort session count. Returns ``None`` on any failure."""
    try:
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        # Scope to the chat surface (chat + legacy null rows) so the count
        # reflects the /chat sidebar, not files / pocket-creation / foresight
        # threads that live in their own rails.
        return await sessions_service.count_for_user(workspace_id, user_id, surface="chat")
    except Exception:
        logger.debug("chat_handler: session count failed", exc_info=True)
        return None


__all__ = ["build_preamble"]
