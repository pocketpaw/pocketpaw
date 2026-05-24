# activity.py — /activity surface preamble.
#
# Created: 2026-05-24 — The user-facing activity feed mirrors the audit
# surface but uses the in-process activity buffer (channel events,
# agent activity) rather than the persisted audit log. Falls back to
# the audit handler's output if the activity buffer isn't reachable.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the activity surface preamble."""
    events = await _load_activity()
    parts = [
        '<surface kind="activity" route="/activity" />',
        f'<activity-snapshot count="{len(events)}" />',
    ]
    if not events:
        parts.append("<activity-list>(no recent activity)</activity-list>")
    else:
        rows = [_format_event(e) for e in events[:LIST_LIMIT]]
        parts.append("<activity-list>\n" + "\n".join(rows) + "\n</activity-list>")
    return truncate_preamble("\n".join(parts))


async def _load_activity() -> list:
    """Pull the activity buffer's recent events. ``[]`` on any failure.

    The activity buffer is a singleton populated by channel adapters and
    the agent loop — it may not be wired in every deploy (e.g. unit
    tests). Tolerate missing imports and empty state silently.
    """
    try:
        from pocketpaw_ee.cloud.activity.buffer import get_buffer

        buf = get_buffer()
        return list(getattr(buf, "events", []) or [])[-LIST_LIMIT:]
    except Exception:
        logger.debug("activity_handler: buffer fetch failed", exc_info=True)
        return []


def _format_event(event) -> str:
    """Pull a sensible label out of an ActivityEvent-shaped object."""
    kind = getattr(event, "kind", "?")
    summary = getattr(event, "summary", None) or getattr(event, "agent", "") or "?"
    return f"- {kind}: {summary}"


__all__ = ["build_preamble"]
