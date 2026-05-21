# Calendar module — external calendar sync.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H-NEW-1).
#
# Changes:
# - H-NEW-1: imported gcalendar events now carry created_by_user_id =
#   ctx.user_id (the syncing user). The external creator isn't always a
#   user in the workspace; the syncing user becomes the local steward
#   and can edit / delete the imported event.
#
# Only Google Calendar is implemented in this PR. Outlook and iCloud are
# placeholders that raise NotImplementedError so a future PR can wire them
# without us pretending they work today. The gcalendar wrapper sits on top
# of pocketpaw.integrations.gcalendar.CalendarClient.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ee.calendar._context import RequestContext
from ee.calendar.domain import Event
from ee.calendar.models import _EventDoc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Google Calendar — implemented
# ---------------------------------------------------------------------------


async def pull_from_gcalendar(ctx: RequestContext, calendar_id: str) -> int:
    """Pull recent events from a Google Calendar into our store.

    Reconciles by (source_connector="gcalendar", source_external_id=<google id>).
    Returns the count of events created OR updated.

    This is a thin wrapper around pocketpaw.integrations.gcalendar.CalendarClient.
    OAuth must already be set up — if it isn't, the underlying client raises
    RuntimeError and we propagate (the caller is responsible for showing the
    OAuth re-auth flow).
    """
    # Lazy import — keeps ee.calendar importable even when the OAuth deps
    # haven't been configured at process start.
    from pocketpaw.integrations.gcalendar import CalendarClient  # type: ignore[import-untyped]

    client = CalendarClient()
    time_min = datetime.now(UTC) - timedelta(days=1)
    time_max = datetime.now(UTC) + timedelta(days=30)

    external_events = await client.list_events(
        time_min=time_min,
        time_max=time_max,
        max_results=250,
        calendar_id=calendar_id,
    )

    touched = 0
    for ext in external_events:
        external_id = ext.get("id") or ""
        if not external_id:
            continue
        try:
            starts_at = _parse_iso(ext.get("start", ""))
            ends_at = _parse_iso(ext.get("end", ""))
        except ValueError:
            logger.warning("Skipping gcalendar event with bad time: %r", ext)
            continue
        if starts_at is None or ends_at is None:
            continue

        attendees = [
            {"email": email, "response": "needs_action", "is_organizer": False}
            for email in ext.get("attendees", [])
            if email
        ]

        existing = await _EventDoc.find_one(
            _EventDoc.workspace == ctx.workspace_id,
            _EventDoc.source_connector == "gcalendar",
            _EventDoc.source_external_id == external_id,
        )
        if existing:
            existing.title = ext.get("summary") or existing.title
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            existing.description = ext.get("description") or ""
            existing.location = ext.get("location") or None
            existing.attendees = attendees
            existing.updated_at = datetime.now(UTC)
            await existing.save()
        else:
            doc = _EventDoc(
                workspace=ctx.workspace_id,
                calendar_id=calendar_id,
                title=ext.get("summary") or "(no title)",
                description=ext.get("description") or "",
                starts_at=starts_at,
                ends_at=ends_at,
                # gcalendar dateTime carries its own offset; we keep UTC as the canonical store.
                timezone="UTC",
                # H-NEW-1: imported events are owned by the user running the
                # sync. We don't try to reconstruct the original gcalendar
                # creator — that user may not exist in the workspace. The
                # syncing user therefore acts as the local steward and can
                # update or delete the imported event.
                created_by_user_id=ctx.user_id,
                location=ext.get("location") or None,
                attendees=attendees,
                source_connector="gcalendar",
                source_external_id=external_id,
            )
            await doc.insert()
        touched += 1

    return touched


async def push_to_gcalendar(ctx: RequestContext, event: Event) -> str:
    """Push a local Event to Google Calendar. Returns the external id.

    Reads the same `pocketpaw.integrations.gcalendar.CalendarClient` used by
    pull; mutation is one-way (this PR doesn't track the local event's
    source_external_id once it's been pushed — the next pull will reconcile).
    """
    # ctx is accepted so future per-workspace OAuth scoping has a home.
    del ctx
    from pocketpaw.integrations.gcalendar import CalendarClient  # type: ignore[import-untyped]

    client = CalendarClient()
    response = await client.create_event(
        summary=event.title,
        start=event.starts_at.isoformat(),
        end=event.ends_at.isoformat(),
        description=event.description,
        location=event.location or "",
        attendees=[a.email for a in event.attendees],
        calendar_id="primary",
    )
    return response.get("id") or ""


# ---------------------------------------------------------------------------
# Outlook / iCloud — placeholders
# ---------------------------------------------------------------------------


async def pull_from_outlook(ctx: RequestContext, calendar_id: str) -> int:  # noqa: ARG001
    """Placeholder. Microsoft Graph integration ships in a follow-up PR."""
    raise NotImplementedError("Outlook sync — future PR")


async def pull_from_icloud(ctx: RequestContext, calendar_id: str) -> int:  # noqa: ARG001
    """Placeholder. CalDAV / iCloud integration ships in a follow-up PR."""
    raise NotImplementedError("iCloud sync — future PR")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime | None:
    """Parse the ISO timestamps returned by gcalendar. Accepts both
    datetime strings (RFC 3339) and date-only strings (treated as midnight)."""
    if not s:
        return None
    try:
        # Python 3.11+ fromisoformat handles 'Z' suffix in 3.11.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
