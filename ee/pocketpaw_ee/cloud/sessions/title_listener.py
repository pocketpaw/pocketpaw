"""Persist backend-generated chat titles into MongoDB.

The pocketpaw message bus emits a ``session_titled`` SystemEvent once per
session (after the first user message) with a Haiku-generated title. The OSS
dashboard consumes it live via SSE, but cloud mode needs to persist the title
to the Session document so it survives a refresh.

This module wires a subscriber at startup; it is a best-effort side-effect
that must never break the chat flow.
"""

from __future__ import annotations

import logging

from pocketpaw.bus import get_message_bus
from pocketpaw.bus.events import SystemEvent

logger = logging.getLogger(__name__)


async def _on_system_event(event: SystemEvent) -> None:
    if event.event_type != "session_titled":
        return
    data = event.data or {}
    session_id = data.get("session_id")
    title = (data.get("title") or "").strip()
    if not session_id or not title:
        logger.info("session_titled event missing session_id/title: %s", data)
        return

    # Lazy import so a bare ``import`` of this module doesn't require
    # Beanie/Motor to be initialized.
    try:
        from pocketpaw_ee.cloud.sessions import service as sessions_service
    except ImportError:
        logger.warning("pocketpaw_ee.cloud sessions service unavailable; skipping title persist")
        return

    if await sessions_service.set_title_if_default(session_id, title):
        logger.info("persisted title for session %s: %r", session_id, title)


_subscribed = False


def register() -> None:
    """Idempotently subscribe the title-persistence handler to the bus."""
    global _subscribed
    if _subscribed:
        return
    get_message_bus().subscribe_system(_on_system_event)
    _subscribed = True
    logger.info("Cloud chat-title listener registered")
