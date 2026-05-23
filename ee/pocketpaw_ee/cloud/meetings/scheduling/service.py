"""MeetingSchedule lifecycle — source-agnostic scheduling helpers.

Phase 1 ships a stub. The real scheduling logic lands in Phase 3 when
the LiveKit engineer rebases #1178 onto the platform — that PR's
``meeting.scheduled`` / ``meeting.reminder`` / auto-start flow already
exists; we just port it here so it's available to both sources.

What lives here (when populated):
  * ``schedule_meeting(ctx, meeting_id, scheduled_start)`` — flips a
    meeting's status to "scheduled" and emits ``MeetingScheduled`` so
    ``bridges.notifications`` fans out a ``meeting_scheduled`` notification.
  * ``start_meeting(ctx, meeting_id)`` — flips to "active", dispatches to
    the provider's ``start()``, emits ``MeetingStarted``.
  * ``end_meeting(ctx, meeting_id)`` — flips to "ended", dispatches to the
    provider's ``end()``, emits ``MeetingEnded``.

The reminder loop in ``reminders.py`` calls into these.
"""

from __future__ import annotations

# Intentionally empty in Phase 1 — see module docstring.
