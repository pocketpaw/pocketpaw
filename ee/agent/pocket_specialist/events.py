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
    # Log first — operators tailing logs see the trace even if the
    # realtime bus has no subscribers (e.g. headless runs, dev shells).
    log.info("[pocket-specialist] %s %s", event.value, _summarize(data))
    try:
        await event_bus.emit(event.value, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("Specialist event emit failed (non-fatal): %s", exc)


def _summarize(data: dict[str, Any]) -> str:
    """Render the data dict as a compact ``key=value`` string for log
    lines. Long string values are trimmed to 80 chars so a noisy brief
    or warning list doesn't blow up the log line."""
    if not data:
        return ""
    parts: list[str] = []
    for k, v in data.items():
        if isinstance(v, str) and len(v) > 80:
            v = v[:77] + "..."
            parts.append(f"{k}={v!r}")
        elif isinstance(v, str):
            parts.append(f"{k}={v!r}")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)
