"""Calendar ↔ Meeting bidirectional bridge.

Forward (calendar → meeting): when a calendar event lands with a
Zoom / Google Meet / Teams URL in its description or location,
automatically create a corresponding Meeting row with ``source="recall"``
and the detected provider. The Recall.ai bot then becomes one click
away from any calendar invite — no manual copy-paste needed.

Reverse (meeting → calendar): when a Meeting is scheduled via /meetings
(or the chat agent, MCP tools, etc.), mint a matching CalendarEvent so
the meeting shows up on /calendar. The calendar event is stamped with
``fabric_object_id="meeting:{meeting_id}"`` so the forward handler can
short-circuit and avoid re-creating a duplicate Meeting.

Subscribes on ``shared.events.event_bus``:

* ``calendar.event.created`` → forward: scan for URL, mint Meeting
  (skipped if ``fabric_object_id`` already links to a meeting we made)
* ``calendar.event.deleted`` → forward: cancel the linked Meeting
* ``meeting.scheduled`` → reverse: mint a CalendarEvent, link both rows
* ``meeting.cancelled`` → reverse: delete the linked CalendarEvent

Updates (``calendar.event.updated`` / silent meeting updates)
intentionally don't sync. Re-detecting URLs on calendar updates risks
double-creating meetings when descriptions are edited for unrelated
reasons; the same logic applies in reverse.
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

    # Reverse-bridge loop guard: this calendar event was minted by us from
    # a Meeting that already exists. Re-detecting the URL would create a
    # second Meeting row pointing at the same call. ``getattr`` keeps the
    # check forward-compatible with test doubles that omit the attribute.
    fabric_link = getattr(doc, "fabric_object_id", None)
    if isinstance(fabric_link, str) and fabric_link.startswith("meeting:"):
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
# Reverse bridge — Meeting → CalendarEvent
# ---------------------------------------------------------------------------

# All Meeting-derived calendar events land on a single synthetic calendar
# so they group cleanly in /calendar UIs (next to the workspace's own
# calendars). Per ``calendar.service._load_calendar``, no row needs to
# exist — the service synthesizes a default Calendar with this name when
# missing, owned by the request actor.
_MEETING_CALENDAR_ID = "meetings"


async def _on_meeting_scheduled(data: dict[str, Any]) -> None:
    """Mint a CalendarEvent so a scheduled Meeting shows up on /calendar."""
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    if not (workspace_id and meeting_id):
        return

    # Forward-bridge loop guard: this Meeting was already minted from a
    # calendar event by ``_on_calendar_event_created``; emitting our own
    # reverse-bridge calendar event would create a second row.
    if data.get("auto_created_from_calendar"):
        return

    try:
        from pocketpaw_ee.calendar._context import RequestContext
        from pocketpaw_ee.calendar.dto import CreateEventRequest
        from pocketpaw_ee.calendar.service import create_event
        from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
    except ImportError:
        logger.warning("calendar package unavailable — reverse bridge disabled")
        return

    doc = await _MeetingDoc.find_one(
        {"workspace": workspace_id, "_id": _maybe_object_id(meeting_id)},
    )
    if doc is None:
        return

    # Idempotency: a second meeting.scheduled emit (retry, bus replay)
    # must not create a second calendar row.
    payload = dict(doc.raw_provider_payload or {})
    if payload.get("calendar_event_id"):
        return

    starts_at = doc.scheduled_start
    if starts_at is None:
        # Meetings without a scheduled_start (e.g. instant calls) don't
        # need a calendar entry; skip silently.
        return
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    ends_at = doc.scheduled_end
    if ends_at is None:
        ends_at = _default_end(starts_at)
    elif ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=UTC)
    # CreateEventRequest enforces ends > starts; nudge by 1 minute if
    # the meeting somehow ended up zero-length.
    if ends_at is None or ends_at <= starts_at:
        ends_at = starts_at + timedelta(minutes=30)

    actor = doc.created_by_user_id or data.get("created_by") or "system"
    ctx = RequestContext(workspace_id=workspace_id, user_id=actor)

    body = CreateEventRequest(
        calendar_id=_MEETING_CALENDAR_ID,
        title=doc.title or "Untitled meeting",
        starts_at=starts_at,
        ends_at=ends_at,
        timezone="UTC",
        description=_format_description(doc),
        location=doc.join_url or None,
        # Loop-prevention marker — _on_calendar_event_created checks this
        # and short-circuits when present.
        fabric_object_id=f"meeting:{meeting_id}",
    )

    try:
        event_resp = await create_event(ctx, body)
    except Exception:  # noqa: BLE001 — bridge must not break meeting create
        logger.exception("reverse bridge: failed to mint CalendarEvent for meeting=%s", meeting_id)
        return

    # Link the meeting back to the new calendar event so the forward
    # bridge sees the linkage and the cancellation handler can find it.
    payload["calendar_event_id"] = event_resp.id
    payload.setdefault("auto_linked_to_calendar", "meeting_bridge")
    doc.raw_provider_payload = payload
    await doc.save()

    logger.info(
        "Reverse bridge: minted CalendarEvent=%s for meeting=%s",
        event_resp.id,
        meeting_id,
    )


async def _on_meeting_cancelled(data: dict[str, Any]) -> None:
    """Delete the CalendarEvent that mirrored a now-cancelled Meeting."""
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    if not (workspace_id and meeting_id):
        return

    try:
        from pocketpaw_ee.calendar._context import RequestContext
        from pocketpaw_ee.calendar.service import delete_event
        from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
    except ImportError:
        return

    doc = await _MeetingDoc.find_one(
        {"workspace": workspace_id, "_id": _maybe_object_id(meeting_id)},
    )
    if doc is None:
        return

    payload = dict(doc.raw_provider_payload or {})
    event_id = payload.get("calendar_event_id")
    auto_linked = payload.get("auto_linked_to_calendar")
    if not event_id or auto_linked != "meeting_bridge":
        # Either we didn't mint this calendar event (forward-bridge case)
        # or there's no link at all. Forward-bridge events are cleaned up
        # via the user's own calendar cancellation, not from here.
        return

    actor = doc.created_by_user_id or "system"
    ctx = RequestContext(workspace_id=workspace_id, user_id=actor)
    try:
        await delete_event(ctx, event_id)
    except Exception:  # noqa: BLE001 — bridge must not break meeting cancel
        logger.exception(
            "reverse bridge: failed to delete CalendarEvent=%s for cancelled meeting=%s",
            event_id,
            meeting_id,
        )


def _format_description(doc: Any) -> str:
    """Compose a calendar event description from meeting metadata.

    Kept terse — the join URL goes in ``location`` so calendar clients
    surface it as a clickable link. Description carries the human label
    and a stable provenance marker.
    """
    lines: list[str] = []
    if doc.provider == "zoom":
        lines.append("Zoom meeting")
    elif doc.provider == "google_meet":
        lines.append("Google Meet")
    elif doc.source == "livekit":
        lines.append("Group call (in-app)")
    else:
        lines.append("Meeting")
    if doc.join_url:
        lines.append(f"Join: {doc.join_url}")
    lines.append("— Synced from Meetings")
    return "\n".join(lines)


def _maybe_object_id(value: str) -> Any:
    """Coerce ``value`` to ``PydanticObjectId`` when possible; else raw."""
    try:
        from beanie import PydanticObjectId

        return PydanticObjectId(value)
    except Exception:  # noqa: BLE001 — bson/beanie raise a variety
        return value


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_meeting_calendar_listeners() -> None:
    """Wire the bidirectional Calendar ↔ Meeting bridge. Idempotent."""
    event_bus.subscribe("calendar.event.created", _on_calendar_event_created)
    event_bus.subscribe("calendar.event.deleted", _on_calendar_event_deleted)
    event_bus.subscribe("meeting.scheduled", _on_meeting_scheduled)
    event_bus.subscribe("meeting.cancelled", _on_meeting_cancelled)
    logger.info("registered calendar ↔ meeting bidirectional bridge")


__all__ = [
    "detect_meeting_url",
    "register_meeting_calendar_listeners",
]
