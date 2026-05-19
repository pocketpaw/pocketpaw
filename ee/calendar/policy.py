# Calendar module — access policy checks.
# Created: 2026-05-19 (feat/calendar-module).
#
# Read/write gates expressed as raise-on-deny helpers (matches the
# ee/cloud convention: services call check_*, the function raises
# Forbidden, or it returns None). Visibility check is a predicate so
# service.py can use it in list filters without try/except.

from __future__ import annotations

from ee.calendar._context import RequestContext
from ee.calendar.domain import Calendar, CalendarVisibility, Event
from ee.cloud.shared.errors import Forbidden


def check_calendar_read(ctx: RequestContext, calendar: Calendar) -> None:
    """Raise Forbidden if the caller can't read this calendar."""
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
    user to shared_with_user_ids to grant write.
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


def check_event_visibility(ctx: RequestContext, event: Event) -> bool:
    """Return True if the caller can see this event.

    Used as a list-filter predicate, NOT a gate — callers that need a hard
    deny should use check_calendar_read on the parent calendar. This check
    is intentionally lighter so list_events can skip without per-event DB
    fetches.
    """
    return event.workspace_id == ctx.workspace_id
