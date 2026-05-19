# Calendar module — free/busy availability computation.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H2).
#
# Changes (H2 — close the availability-oracle):
# - compute_freebusy now accepts ``accessible_calendar_ids``: an optional
#   set restricting which calendars contribute to the returned busy
#   periods. Callers that have already verified the requester can read
#   those calendars pass the resolved set; events on calendars outside
#   the set are silently dropped from the result rather than appearing
#   as a busy block. None preserves the legacy behaviour for callers
#   that aren't ready to enforce yet.
# - The function itself doesn't validate the attendee list — the service
#   layer is the chokepoint for the "is this email known on a calendar
#   I can see?" pre-validation (raises ValidationError before we get
#   here).
#
# Given a workspace, a list of attendee emails, and a window, returns the
# union of busy periods for each attendee. Single Mongo query with $in on
# the embedded attendees.email — cheaper than N queries.

from __future__ import annotations

from datetime import datetime

from ee.calendar.domain import FreeBusy
from ee.calendar.models import _EventDoc


async def compute_freebusy(
    workspace_id: str,
    attendee_emails: list[str],
    starts_at: datetime,
    ends_at: datetime,
    accessible_calendar_ids: set[str] | None = None,
) -> list[FreeBusy]:
    """Return per-attendee busy periods within [starts_at, ends_at).

    ``accessible_calendar_ids`` (H2): when provided, only events whose
    ``calendar_id`` is in the set contribute to the returned busy
    periods. This closes the availability oracle — a requester only
    sees busy blocks for calendars they could already read directly.
    ``None`` keeps the legacy behaviour (return everything in the
    workspace) and is reserved for trusted internal callers.

    Recurring events: this implementation queries the master rows only,
    which means recurring instances inside the window will appear as a
    single busy block (the master). A future PR will plug in
    recurrence.expand_recurrence here. Documented in the module docstring
    so the gap is explicit rather than silent.
    """
    if not attendee_emails:
        return []

    if ends_at <= starts_at:
        return [FreeBusy(attendee_email=e, busy_periods=[]) for e in attendee_emails]

    # Lowercase emails for case-insensitive matching against stored data.
    emails_lower = [e.lower() for e in attendee_emails]

    overlapping = await _EventDoc.find(
        {
            "workspace": workspace_id,
            "starts_at": {"$lt": ends_at},
            "ends_at": {"$gt": starts_at},
            "attendees.email": {"$in": emails_lower},
        }
    ).to_list()

    # Build per-attendee busy lists. Preserve the caller's email casing in
    # the output so the response matches what they sent.
    busy_by_email: dict[str, list[tuple[datetime, datetime]]] = {
        e.lower(): [] for e in attendee_emails
    }

    for doc in overlapping:
        # H2: skip events on calendars the caller can't read.
        if (
            accessible_calendar_ids is not None
            and getattr(doc, "calendar_id", None) not in accessible_calendar_ids
        ):
            continue
        # Clip to window so callers don't see periods outside what they asked for.
        clipped_start = max(doc.starts_at, starts_at)
        clipped_end = min(doc.ends_at, ends_at)
        if clipped_end <= clipped_start:
            continue
        for att in doc.attendees or []:
            email = (att.get("email") or "").lower()
            if email in busy_by_email:
                busy_by_email[email].append((clipped_start, clipped_end))

    # Sort each list by start time for deterministic output.
    result: list[FreeBusy] = []
    for original_email in attendee_emails:
        periods = sorted(busy_by_email[original_email.lower()], key=lambda p: p[0])
        result.append(FreeBusy(attendee_email=original_email, busy_periods=periods))

    return result
