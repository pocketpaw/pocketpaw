"""Calendar events → Meeting auto-creation.

When a calendar event lands with a Zoom / Google Meet / Teams URL in its
description or location, automatically create a corresponding Meeting
row with ``source="recall"`` and the detected provider. The Recall.ai
bot then becomes one click away from any calendar invite — no manual
copy-paste of a meeting URL needed.

Subscribes to ``calendar.event.created`` and ``calendar.event.deleted``
on ``shared.events.event_bus``:

* **created** → load the event doc, detect a meeting URL, mint a
  ``MeetingDoc`` with the detected provider and the calendar event id
  recorded under ``raw_provider_payload.calendar_event_id`` so we can
  deduplicate on update + clean up on delete.
* **deleted** → look up the linked meeting (by ``calendar_event_id``)
  and mark it cancelled. The Recall bot, if dispatched, gets the leave
  signal via the existing stop-bot path on cancellation.

Updates (``calendar.event.updated``) intentionally don't re-detect URLs
— if someone adds a Zoom link to an existing event after the fact, they
can dispatch a bot manually. Auto-recreating on update risks
double-creating meetings when descriptions are edited for unrelated
reasons.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

# Conservative regexes — we'd rather miss a malformed URL than false-positive
# and create a Meeting against the wrong join URL. Each pattern returns the
# full URL plus the detected provider name.
_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "zoom",
        re.compile(
            r"https?://[a-zA-Z0-9.-]*zoom\.us/(?:j|my|w)/[A-Za-z0-9?=&._-]+",
            re.IGNORECASE,
        ),
    ),
    (
        "google_meet",
        re.compile(
            r"https?://meet\.google\.com/[a-z0-9-]+",
            re.IGNORECASE,
        ),
    ),
    # Teams URLs are long and contain query strings; capture aggressively.
    (
        "teams",
        re.compile(
            r"https?://teams\.microsoft\.com/l/meetup-join/[^\s\"'<>]+",
            re.IGNORECASE,
        ),
    ),
]


def detect_meeting_url(text: str) -> tuple[str, str] | None:
    """Return ``(provider, url)`` if ``text`` contains a known meeting URL.

    Returns ``None`` when no URL is found, or when the only matches are
    for providers we can't auto-create against (e.g. Teams — Recall
    supports it but our adapter layer doesn't yet).
    """
    if not text:
        return None
    for provider, pattern in _PROVIDER_PATTERNS:
        match = pattern.search(text)
        if match:
            return provider, match.group(0)
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _on_calendar_event_created(data: dict[str, Any]) -> None:
    """Scan the calendar event for a meeting URL; auto-create a Meeting."""
    event_id = data.get("event_id")
    workspace_id = data.get("workspace_id")
    if not (event_id and workspace_id):
        return

    # Late import — calendar package is enterprise-only, mirrors meetings.
    try:
        from pocketpaw_ee.calendar.models import _EventDoc
    except ImportError:
        logger.warning("calendar models unavailable — calendar bridge disabled")
        return

    doc = await _EventDoc.find_one({"workspace": workspace_id, "_id": event_id})
    if doc is None:
        return

    haystack = " ".join(filter(None, [doc.description, doc.location, doc.title]))
    detected = detect_meeting_url(haystack)
    if detected is None:
        return  # Calendar event without a meeting URL → not a meeting we capture.
    provider, join_url = detected

    if provider == "teams":
        # Recall supports Teams capture but our adapter factory only knows
        # zoom + google_meet today. Skip auto-creation; the user can still
        # dispatch a bot manually via the API.
        logger.info(
            "Skipping auto-create for Teams URL on calendar event=%s — manual dispatch only",
            event_id,
        )
        return

    await _auto_create_meeting(
        workspace_id=workspace_id,
        calendar_event_id=event_id,
        provider=provider,
        join_url=join_url,
        title=doc.title or "Untitled meeting",
        scheduled_start=doc.starts_at,
        scheduled_end=doc.ends_at,
        created_by_user_id=doc.created_by_user_id,
    )


async def _on_calendar_event_deleted(data: dict[str, Any]) -> None:
    """Cancel the meeting that was auto-created for this calendar event."""
    event_id = data.get("event_id")
    workspace_id = data.get("workspace_id")
    if not (event_id and workspace_id):
        return

    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

    doc = await _MeetingDoc.find_one(
        {
            "workspace": workspace_id,
            "raw_provider_payload.calendar_event_id": event_id,
            "status": {"$ne": "cancelled"},
        }
    )
    if doc is None:
        return

    doc.status = "cancelled"
    # no-event: emitting `meeting.cancelled` here would notify the
    # creator a second time after they just cancelled the calendar
    # invite; intentional silence.
    await doc.save()
    logger.info("Cancelled meeting=%s after calendar event=%s deletion", doc.id, event_id)


# ---------------------------------------------------------------------------
# Auto-create — writes a MeetingDoc bypassing the provider's create()
# because the third-party meeting already exists; we're just recording it.
# ---------------------------------------------------------------------------


async def _auto_create_meeting(
    *,
    workspace_id: str,
    calendar_event_id: str,
    provider: str,
    join_url: str,
    title: str,
    scheduled_start: datetime | None,
    scheduled_end: datetime | None,
    created_by_user_id: str | None,
) -> None:
    """Insert a MeetingDoc for an external meeting we discovered via calendar.

    Idempotent: if a meeting already exists for this calendar event id,
    do nothing. Avoids duplicate rows when calendar.event.created fires
    twice (sync collision, user edit, etc.).
    """
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
    from pocketpaw_ee.cloud.shared.events import event_bus as _bus

    existing = await _MeetingDoc.find_one(
        {
            "workspace": workspace_id,
            "raw_provider_payload.calendar_event_id": calendar_event_id,
        }
    )
    if existing is not None:
        return

    doc = _MeetingDoc(
        workspace=workspace_id,
        source="recall",
        provider=provider,  # type: ignore[arg-type]
        provider_meeting_id="",  # unknown — we only have the join URL
        title=title,
        join_url=join_url,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end or _default_end(scheduled_start),
        status="scheduled",
        participants=[],
        recording_file_ids=[],
        raw_provider_payload={
            "calendar_event_id": calendar_event_id,
            "auto_created_by": "calendar_bridge",
        },
        created_by_user_id=created_by_user_id,
    )
    await doc.insert()

    await _bus.emit(
        "meeting.scheduled",
        {
            "workspace_id": workspace_id,
            "meeting_id": str(doc.id),
            "source": "recall",
            "provider": provider,
            "created_by": created_by_user_id or "calendar_bridge",
            "auto_created_from_calendar": True,
        },
    )
    logger.info(
        "Auto-created meeting=%s from calendar event=%s (%s)",
        doc.id,
        calendar_event_id,
        provider,
    )


def _default_end(start: datetime | None) -> datetime | None:
    """30-minute default duration when the calendar event has no end."""
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return start + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_meeting_calendar_listeners() -> None:
    """Wire calendar.event.* → meeting auto-create. Idempotent."""
    event_bus.subscribe("calendar.event.created", _on_calendar_event_created)
    event_bus.subscribe("calendar.event.deleted", _on_calendar_event_deleted)
    logger.info("registered calendar → meeting auto-create subscribers")


__all__ = [
    "detect_meeting_url",
    "register_meeting_calendar_listeners",
]
