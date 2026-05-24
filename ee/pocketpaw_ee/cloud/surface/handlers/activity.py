# activity.py — /activity surface preamble.
#
# Updated: 2026-05-24 — Added workspace_id tenancy guard and switched to
# the buffer's workspace-scoped read API. The previous implementation
# read ``getattr(buf, "events", [])`` — an attribute the singleton never
# exposed (the buffer's API is ``get_recent(workspace_id, limit)``) so
# the list was always empty in production. Worse, if a future refactor
# had exposed a flat ``events`` list it would have leaked every
# workspace's activity into every workspace's chat preamble. We now
# call ``get_buffer().get_recent(workspace_id, ...)`` directly, which is
# the only workspace-scoped read path the buffer supports. When the
# buffer can't be filtered (import error, missing API), the handler
# emits a placeholder list instead of a cross-workspace event dump.
#
# Original: The user-facing activity feed mirrors the audit surface but
# uses the in-process activity buffer (channel events, agent activity)
# rather than the persisted audit log.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)

LIST_LIMIT = 10


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the activity surface preamble."""
    events, scoped = await _load_activity(workspace_id)
    parts = [
        '<surface kind="activity" route="/activity" />',
        f'<activity-snapshot count="{len(events)}" />',
    ]
    if not scoped:
        # Buffer isn't workspace-scoped in this deploy — emit a
        # placeholder rather than risk leaking another workspace's
        # events into this chat.
        parts.append(
            "<activity-list>(activity unavailable — per-workspace scope required)</activity-list>"
        )
    elif not events:
        parts.append("<activity-list>(no recent activity)</activity-list>")
    else:
        rows = [_format_event(e) for e in events[:LIST_LIMIT]]
        parts.append("<activity-list>\n" + "\n".join(rows) + "\n</activity-list>")
    return truncate_preamble("\n".join(parts))


async def _load_activity(workspace_id: str) -> tuple[list, bool]:
    """Return ``(events, scoped)`` for ``workspace_id``.

    ``scoped`` is ``True`` only when the buffer exposed a workspace-aware
    read path and we successfully used it. Any other case (missing
    import, buffer without ``get_recent``, exception) returns
    ``([], False)`` so the handler can render the placeholder rather
    than guessing whether the empty list reflects no activity or a
    missing filter.
    """
    try:
        from pocketpaw_ee.cloud.activity.buffer import get_buffer

        buf = get_buffer()
        get_recent = getattr(buf, "get_recent", None)
        if not callable(get_recent):
            # Buffer is reachable but doesn't expose the workspace-scoped
            # API. Treat as "not scoped" — don't fall back to a flat read.
            return [], False
        events = list(get_recent(workspace_id, LIST_LIMIT) or [])
        return events, True
    except Exception:
        logger.debug("activity_handler: buffer fetch failed", exc_info=True)
        return [], False


def _format_event(event) -> str:
    """Pull a sensible label out of an ActivityEvent-shaped object."""
    kind = getattr(event, "kind", "?")
    summary = getattr(event, "summary", None) or getattr(event, "agent", "") or "?"
    return f"- {kind}: {summary}"


__all__ = ["build_preamble"]
