"""Thin facade services call after successful mutations.

``emit`` never raises back to the caller. The underlying bus publishes
best-effort; a failure here must not abort the DB write that preceded it.
The one exception is if the bus has never been initialized — that's a
programmer error (probably forgot to call ``init_realtime`` in startup) and
surfaces as ``AssertionError`` so it's caught immediately in tests.
"""

from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import Event

logger = logging.getLogger(__name__)


async def emit(event: Event) -> None:
    bus = get_bus()  # raises AssertionError if not initialized (programmer error)
    try:
        await bus.publish(event)
    except Exception:
        logger.exception("emit failed for event %s", event.type)
