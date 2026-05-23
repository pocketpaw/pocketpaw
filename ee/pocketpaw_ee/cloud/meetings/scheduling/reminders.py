"""60-second reminder loop — auto-start + 5-minute-ahead reminders.

Phase 1 ships a stub. The real loop lands in Phase 3 when the LiveKit
engineer rebases #1178 — that PR's ``_reminder_loop`` already does the
right thing, and is source-agnostic (it just calls ``service.start_meeting``
and emits ``MeetingReminder`` regardless of provider).

When populated:
  * Background task started from the dashboard lifespan.
  * Ticks every 60s. Queries ``MeetingDoc.find({status: "scheduled",
    scheduled_start: <next 70s window>})`` and auto-transitions matched
    rows to ``active`` via ``service.start_meeting``.
  * Separately queries the 5-minute window and emits ``MeetingReminder``
    (one per meeting, deduped via ``provider_payload["reminder_sent"]``).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _reminder_loop() -> None:
    """Background task — implemented in Phase 3 (LiveKit rebase of #1178)."""
    while True:
        await asyncio.sleep(60)
        # TODO(phase-3): tick body lands when #1178 rebases.


async def start_reminder_loop() -> None:
    """Called from dashboard startup. Phase 1 = no-op; logs a warning so
    operators know the loop isn't running."""
    logger.warning(
        "meetings reminder loop is a Phase 1 stub — scheduled meetings "
        "will NOT auto-start until #1178 rebases on this platform."
    )


async def stop_reminder_loop() -> None:
    """Called from dashboard shutdown. Phase 1 no-op."""
    return None
