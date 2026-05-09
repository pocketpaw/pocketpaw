"""Status events emitted during a pocket specialist run.

Best-effort fire-and-forget emission to the realtime bus. Bus failures
NEVER propagate — the specialist's work continues even when no client
is subscribed.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from ee.cloud.shared.events import event_bus

log = logging.getLogger(__name__)


class SpecialistEvent(str, Enum):
    """Status event names. Frontend consumes these as progress indicators."""

    START = "specialist:start"
    LISTING = "specialist:listing"
    DECIDED = "specialist:decided"
    DRAFTING = "specialist:drafting"
    VALIDATING = "specialist:validating"
    REVISING = "specialist:revising"
    PERSISTING = "specialist:persisting"
    DONE = "specialist:done"


async def emit_specialist_event(
    event: SpecialistEvent,
    data: dict[str, Any],
) -> None:
    """Emit a specialist status event. Best-effort — never raises."""
    try:
        await event_bus.emit(event.value, data)
    except Exception as exc:  # noqa: BLE001
        log.warning("Specialist event emit failed (non-fatal): %s", exc)
