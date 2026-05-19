# Calendar module — access policy checks.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H1).
#
# Changes:
# - Read/write gates now reused by service.py on every CRUD path. The
#   functions were previously defined but never called — H1 of the
#   #1132 audit. The semantics here are unchanged, but the doc clarifies
#   the contract callers see (owner OR explicit share OR public read).
# - Added check_calendar_freebusy as an explicit predicate (returns bool,
#   no raise) so service.compute_freebusy can filter events without
#   per-call try/except.
# - Workspace-admin override is intentionally out of scope: there is no
#   workspace-role plumbing yet in ee/calendar. Tracked as a follow-up.
#
# Read/write gates are expressed as raise-on-deny helpers (matches the
# ee/cloud convention: services call check_*, the function raises
# Forbidden, or it returns None). Visibility check is a predicate so
# callers can use it in list filters without try/except.

from __future__ import annotations

from ee.calendar._context import RequestContext
from ee.calendar.domain import Calendar, CalendarVisibility, Event
from ee.cloud.shared.errors import Forbidden


def check_calendar_read(ctx: RequestContext, calendar: Calendar) -> None:
    """Raise Forbidden if the caller can't read this calendar.

    Read access is granted when ANY of the following is true:
      1. The calendar's workspace matches the caller's workspace AND
      2a. The caller is the calendar owner, OR
      2b. The calendar is workspace-public, OR
      2c. The caller is in ``shared_with_user_ids``.

    Otherwise Forbidden is raised. Different-workspace lookups always
    fail closed — this is the tenant boundary.
    """
    # Hard tenant boundary — never let a different-workspace caller read at all.
    if calendar.workspace_id != ctx.workspace_id:
        raise Forbidden(
            "calendar.access_denied",
            "Calendar belongs to a different workspace",
        )
    if calendar.owner_user_id == ctx.user_id:
        return
    if calendar.visibility == CalendarVisibility.PUBLIC_TO_WORKSPACE:
        return
    if (
        calendar.visibility == CalendarVisibility.SHARED_WITH_USERS
        and ctx.user_id in calendar.shared_with_user_ids
    ):
        return
    raise Forbidden(
        "calendar.access_denied",
        "You do not have read access to this calendar",
    )


def check_calendar_write(ctx: RequestContext, calendar: Calendar) -> None:
    """Raise Forbidden if the caller can't write to this calendar.

    Stricter than read — only the owner or an explicitly-shared user can
    write. Workspace-public calendars are read-only by default; promote a
    user to ``shared_with_user_ids`` to grant write.
    """
    if calendar.workspace_id != ctx.workspace_id:
        raise Forbidden(
            "calendar.access_denied",
            "Calendar belongs to a different workspace",
        )
    if calendar.owner_user_id == ctx.user_id:
        return
    if (
        calendar.visibility == CalendarVisibility.SHARED_WITH_USERS
        and ctx.user_id in calendar.shared_with_user_ids
    ):
        return
    raise Forbidden(
        "calendar.access_denied",
        "You do not have write access to this calendar",
    )


def can_read_calendar(ctx: RequestContext, calendar: Calendar) -> bool:
    """Predicate form of check_calendar_read. Use in list filters and the
    freebusy attendee-access check, where a False answer means "skip" not
    "abort"."""
    try:
        check_calendar_read(ctx, calendar)
    except Forbidden:
        return False
    return True


def check_event_visibility(ctx: RequestContext, event: Event) -> bool:
    """Return True if the caller can see this event.

    Used as a list-filter predicate, NOT a gate — callers that need a hard
    deny should use check_calendar_read on the parent calendar. This check
    is intentionally lighter so list_events can skip without per-event DB
    fetches.
    """
    return event.workspace_id == ctx.workspace_id
