# Calendar module — external calendar sync.
# Updated: 2026-08-06 (feat/coupling-calendar-sot, T-13 + review round 2).
#
# Changes:
# - T-13: added ``ingest_composio_events`` — the Composio Google Calendar
#   pull (used by the agent preamble's ``cloud.calendar.list_upcoming``)
#   now reconciles into ``_EventDoc`` through the same
#   ``source_connector`` + ``source_external_id`` keys the native
#   gcalendar pull uses, so /calendar and the agent read one store.
# - T-13 review round 2 (CRITICAL fix): Composio connections are
#   PER-USER (``composio_user_id`` namespaces enterprise:user), so what
#   the ingest lands is one member's personal Google feed. Ingested rows
#   now live on a per-user calendar backed by a real ``_CalendarDoc``
#   with ``visibility="private"``, owner = the syncing user, auto-created
#   on first ingest (``_ensure_composio_calendar``). The existing policy
#   machinery then keeps member A's personal events out of member B's
#   /calendar and agent preamble. (The first cut used the synthetic
#   workspace-public "primary" calendar — a cross-member privacy leak.)
# - T-13: cross-connector dedupe, scoped per-user for Composio rows. The
#   Google event id (``source_external_id``) is the upstream truth, so
#   reconciliation for the google family matches EITHER the native
#   "gcalendar" connector (workspace-visible, single-OAuth legacy) OR a
#   "composio_google" row synced BY THE SAME USER. Two members invited to
#   the same meeting each get their own private row — deduping those
#   across users would strand member B's copy on member A's private
#   calendar where B can't see it. Within one user, whichever route
#   ingests first stamps its connector; the other route UPDATES that row
#   in place and never creates a second one. ``pull_from_gcalendar``
#   uses the same matcher so the guarantee holds in both ingest orders.
# - T-13: timezone capture. Google's ``start.timeZone`` (IANA) is stored
#   on the row when present so the preamble can render event-local wall
#   time; otherwise the store keeps the "UTC" canonical stamp the native
#   pull established.
# - H-NEW-1 (2026-05-19): imported gcalendar events carry
#   created_by_user_id = ctx.user_id (the syncing user acts as steward).
#
# Only Google Calendar is implemented. Outlook and iCloud are placeholders
# that raise NotImplementedError so a future PR can wire them without us
# pretending they work today. The gcalendar wrapper sits on top of
# pocketpaw.clients.gcalendar.CalendarClient.
#
# Sync ingests are silent on the event bus — mirroring the original
# gcalendar pull, neither path emits ``calendar.event.created`` per
# imported row (a 250-row pull would flood the meeting bridge and
# notifications). If bridge coverage for synced events is wanted, that's
# a deliberate follow-up, not an accident of this module.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pocketpaw_ee.calendar._context import RequestContext
from pocketpaw_ee.calendar.domain import Event
from pocketpaw_ee.calendar.models import _CalendarDoc, _EventDoc

logger = logging.getLogger(__name__)

# Connector slugs for the two routes that pull the same upstream Google
# calendar. They share ``source_external_id`` (the Google event id), so
# reconciliation treats them as one family — see _find_google_event.
SOURCE_CONNECTOR_GCALENDAR = "gcalendar"
SOURCE_CONNECTOR_COMPOSIO = "composio_google"
_GOOGLE_CONNECTOR_FAMILY = [SOURCE_CONNECTOR_GCALENDAR, SOURCE_CONNECTOR_COMPOSIO]

# Name marker for the per-user private calendar that Composio-synced
# events land on. The (workspace, owner_user_id, name) triple is the
# lookup key; the calendar's real id is its ObjectId.
_COMPOSIO_CALENDAR_NAME = "Google Calendar (Composio)"


async def _find_google_event(workspace_id: str, external_id: str, user_id: str) -> _EventDoc | None:
    """Find the local row for a Google event id, for cross-connector dedupe.

    Matches (a) native "gcalendar" rows — workspace-visible, from the
    legacy single-OAuth pull — for ANY user, and (b) "composio_google"
    rows synced by THIS user (``created_by_user_id``). Composio rows are
    per-user private data, so another member's row for the same upstream
    event must not be matched: each invitee keeps their own private copy.
    """
    return await _EventDoc.find_one(
        {
            "workspace": workspace_id,
            "source_external_id": external_id,
            "$or": [
                {"source_connector": SOURCE_CONNECTOR_GCALENDAR},
                {
                    "source_connector": SOURCE_CONNECTOR_COMPOSIO,
                    "created_by_user_id": user_id,
                },
            ],
        }
    )


async def _ensure_composio_calendar(ctx: RequestContext) -> str:
    """Resolve (creating on first ingest) the per-user PRIVATE calendar
    that Composio-synced events land on. Returns its id.

    The Composio connection is per-user, so its events are one member's
    personal feed — landing them on the synthetic workspace-public
    default calendar would render them in every other member's /calendar
    and agent preamble. A real ``_CalendarDoc`` with
    ``visibility="private"`` and owner = the syncing user lets the
    existing policy machinery (``check_calendar_read`` /
    ``can_read_calendar``) do the filtering.
    """
    existing = await _CalendarDoc.find_one(
        {
            "workspace": ctx.workspace_id,
            "owner_user_id": ctx.user_id,
            "name": _COMPOSIO_CALENDAR_NAME,
        }
    )
    if existing is not None:
        return str(existing.id)

    doc = _CalendarDoc(
        workspace=ctx.workspace_id,
        name=_COMPOSIO_CALENDAR_NAME,
        owner_user_id=ctx.user_id,
        timezone="UTC",
        visibility="private",
    )
    await doc.insert()
    logger.info(
        "calendar.sync: created private composio calendar id=%s for user=%s",
        doc.id,
        ctx.user_id,
    )
    return str(doc.id)


# ---------------------------------------------------------------------------
# Google Calendar — implemented
# ---------------------------------------------------------------------------


async def pull_from_gcalendar(ctx: RequestContext, calendar_id: str) -> int:
    """Pull recent events from a Google Calendar into our store.

    Reconciles by (source_connector="gcalendar", source_external_id=<google id>).
    Returns the count of events created OR updated.

    This is a thin wrapper around pocketpaw.clients.gcalendar.CalendarClient.
    OAuth must already be set up — if it isn't, the underlying client raises
    RuntimeError and we propagate (the caller is responsible for showing the
    OAuth re-auth flow).
    """
    # Lazy import — keeps ee.calendar importable even when the OAuth deps
    # haven't been configured at process start.
    from pocketpaw.clients.gcalendar import CalendarClient  # type: ignore[import-untyped]

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

        # T-13: match the google connector family (native rows for any
        # user, composio rows for THIS user), not just "gcalendar" — if
        # Composio ingested this event first, update that row instead of
        # minting a duplicate. The row keeps its original connector tag
        # (provenance is who ingested first; both routes keep it fresh).
        existing = await _find_google_event(ctx.workspace_id, external_id, ctx.user_id)
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
                source_connector=SOURCE_CONNECTOR_GCALENDAR,
                source_external_id=external_id,
            )
            await doc.insert()
        touched += 1

    return touched


async def ingest_composio_events(
    ctx: RequestContext,
    items: list[dict[str, Any]],
) -> int:
    """Reconcile raw Google-shaped event items (from Composio's
    ``GOOGLECALENDAR_LIST_EVENTS``) into our store.

    ``items`` are Google API ``events.list`` rows: ``id``, ``summary``,
    ``start.dateTime`` / ``start.date``, ``end.*``, ``attendees`` as a
    list of ``{"email": ...}`` dicts. The transport (Composio client,
    user-id namespacing) stays in ``pocketpaw_ee.cloud.calendar.service``
    — this function owns only the store reconciliation, so it can be
    tested without a Composio double.

    New rows land on the syncing user's PRIVATE composio calendar
    (``_ensure_composio_calendar`` — the Composio connection is per-user,
    so its events are personal data; the private calendar keeps them out
    of other members' /calendar and preamble via the existing policy).

    Reconciles via ``_find_google_event`` — native gcalendar rows for any
    user, composio rows for this user only; see the cross-connector
    dedupe note at the top of this module. New rows are stamped
    ``source_connector="composio_google"``; rows the native gcalendar
    pull minted first keep their "gcalendar" tag and are updated in place.

    Returns the count of events created OR updated.
    """
    calendar_id: str | None = None  # resolved lazily — only when a new row lands
    touched = 0
    for raw in items:
        external_id = raw.get("id")
        if not isinstance(external_id, str) or not external_id:
            continue

        starts_at = _parse_google_time(raw.get("start"))
        ends_at = _parse_google_time(raw.get("end"))
        if starts_at is None or ends_at is None:
            logger.warning("Skipping composio event with bad time: id=%r", external_id)
            continue
        if ends_at <= starts_at:
            # _EventDoc has no window validator, but the domain layer and
            # every write path enforce starts < ends — don't let a bad
            # upstream row poison reads.
            logger.warning("Skipping composio event with inverted window: id=%r", external_id)
            continue

        attendees = [
            {"email": email, "response": "needs_action", "is_organizer": False}
            for email in _attendee_emails(raw.get("attendees"))
        ]
        explicit_tz = _google_timezone(raw)

        existing = await _find_google_event(ctx.workspace_id, external_id, ctx.user_id)
        if existing:
            existing.title = raw.get("summary") or existing.title
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            existing.description = raw.get("description") or ""
            existing.location = raw.get("location") or None
            existing.attendees = attendees
            if explicit_tz is not None:
                # Upgrade the row's timezone only when Google named one —
                # never clobber a real IANA zone with the UTC fallback.
                existing.timezone = explicit_tz
            existing.updated_at = datetime.now(UTC)
            # no-event: sync ingests are silent on the bus (see module header).
            await existing.save()
        else:
            if calendar_id is None:
                calendar_id = await _ensure_composio_calendar(ctx)
            doc = _EventDoc(
                workspace=ctx.workspace_id,
                calendar_id=calendar_id,
                title=raw.get("summary") or "(no title)",
                description=raw.get("description") or "",
                starts_at=starts_at,
                ends_at=ends_at,
                # UTC datetimes are the canonical store (same call as the
                # gcalendar pull); the timezone field carries the event's
                # IANA zone when Google named one, so read paths can
                # render event-local wall time.
                timezone=explicit_tz or "UTC",
                # H-NEW-1 pattern: the user whose Composio connection
                # fetched the event becomes the local steward.
                created_by_user_id=ctx.user_id,
                location=raw.get("location") or None,
                attendees=attendees,
                source_connector=SOURCE_CONNECTOR_COMPOSIO,
                source_external_id=external_id,
            )
            # no-event: sync ingests are silent on the bus (see module header).
            await doc.insert()
        touched += 1

    return touched


def _parse_google_time(block: Any) -> datetime | None:
    """Parse a Google API ``start`` / ``end`` block into a datetime.

    ``{"dateTime": <RFC 3339>}`` for timed events, ``{"date": "YYYY-MM-DD"}``
    for all-day events (parsed as midnight, matching _parse_iso's
    date-only handling)."""
    if not isinstance(block, dict):
        return None
    raw = block.get("dateTime") or block.get("date") or ""
    if not isinstance(raw, str):
        return None
    return _parse_iso(raw)


def _google_timezone(raw: dict[str, Any]) -> str | None:
    """Extract a valid IANA timezone from a Google event item, if named.

    Google puts ``timeZone`` on the ``start`` / ``end`` blocks (required
    for recurring events, common on UI-created ones). Returns ``None``
    when absent or not a resolvable IANA name — callers fall back to the
    store's "UTC" canonical stamp rather than persisting garbage.
    """
    for key in ("start", "end"):
        block = raw.get(key)
        if not isinstance(block, dict):
            continue
        tz_name = block.get("timeZone")
        if isinstance(tz_name, str) and tz_name:
            try:
                ZoneInfo(tz_name)
            except Exception:  # noqa: BLE001 — zoneinfo raises several types
                logger.warning("Ignoring unknown timezone %r on composio event", tz_name)
                return None
            return tz_name
    return None


def _attendee_emails(raw: Any) -> list[str]:
    """Extract plain emails from Google's attendees list-of-dicts shape."""
    emails: list[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                email = entry.get("email")
                if isinstance(email, str) and email:
                    emails.append(email)
    return emails


async def push_to_gcalendar(ctx: RequestContext, event: Event) -> str:
    """Push a local Event to Google Calendar. Returns the external id.

    Reads the same `pocketpaw.clients.gcalendar.CalendarClient` used by
    pull; mutation is one-way (this PR doesn't track the local event's
    source_external_id once it's been pushed — the next pull will reconcile).
    """
    # ctx is accepted so future per-workspace OAuth scoping has a home.
    del ctx
    from pocketpaw.clients.gcalendar import CalendarClient  # type: ignore[import-untyped]

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
