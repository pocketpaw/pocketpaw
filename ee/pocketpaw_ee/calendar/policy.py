# Calendar module — access policy checks.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H1 + H-NEW-1).
#
# Changes:
# - Read/write gates now reused by service.py on every CRUD path. The
#   functions were previously defined but never called — H1 of the
#   #1132 audit. The semantics here are unchanged, but the doc clarifies
#   the contract callers see (owner OR explicit share OR public read).
# - Added check_calendar_freebusy as an explicit predicate (returns bool,
#   no raise) so service.compute_freebusy can filter events without
#   per-call try/except.
# - H-NEW-1: added check_event_modify(ctx, event). The synthetic-default
#   Calendar that _load_calendar falls back to (until Calendar CRUD ships)
#   sets owner_user_id = ctx.user_id, which makes check_calendar_write
#   pass for every workspace member. That re-opens the original H1 hole
#   on update_event / delete_event: any workspace member could mutate any
#   other member's event when the parent Calendar row didn't exist yet.
#   The new gate requires event.created_by_user_id == ctx.user_id (or, in
#   a follow-up, the caller being a workspace admin) before update/delete
#   proceeds. create_event keeps using check_calendar_write only — there
#   is no existing event ownership for it to bypass.
# - Workspace-admin override is intentionally out of scope: there is no
#   workspace-role plumbing yet in ee/calendar's RequestContext. Tracked
#   as a follow-up so the new check_event_modify carries an explicit TODO
#   rather than silently denying every admin caller forever.
#
# Read/write gates are expressed as raise-on-deny helpers (matches the
# ee/cloud convention: services call check_*, the function raises
# Forbidden, or it returns None). Visibility check is a predicate so
# callers can use it in list filters without try/except.

from __future__ import annotations

from pocketpaw_ee.calendar._context import RequestContext
from pocketpaw_ee.calendar.domain import Calendar, CalendarVisibility, Event
from pocketpaw_ee.cloud.shared.errors import Forbidden


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


def check_event_modify(ctx: RequestContext, event: Event) -> None:
    """Raise Forbidden if the caller can't modify this specific event.

    This is the event-level companion to check_calendar_write. It exists
    to close H-NEW-1: while check_calendar_write decides who can write
    anywhere on a calendar, this decides who can edit a particular event
    once it exists. The combination matters because service._load_calendar
    falls back to a synthetic default Calendar (owner = caller) when no
    backing _CalendarDoc row exists yet, which makes check_calendar_write
    a no-op for the synthetic-default path. update_event and delete_event
    therefore call BOTH check_calendar_write AND this function — the
    second one prevents one workspace member from mutating another
    member's events on a synthetic calendar.

    Modify access is granted when ANY of the following is true:
      1. The caller is the event's creator
         (``event.created_by_user_id == ctx.user_id``), OR
      2. The caller is a workspace admin (TODO — admin path not yet
         plumbed through RequestContext; see comment below).

    Otherwise Forbidden is raised. The tenant boundary is assumed to be
    enforced upstream (by ``_get_event_doc_or_404``'s tenant filter), so
    we don't re-check ``event.workspace_id`` here — but a defensive guard
    catches the case where a caller hand-builds an Event from another
    workspace.
    """
    # Defensive tenant guard. _get_event_doc_or_404 already filters on
    # workspace, but if a caller constructs an Event directly (tests, future
    # callers) this still fails closed.
    if event.workspace_id != ctx.workspace_id:
        raise Forbidden(
            "event.modify_denied",
            "Event belongs to a different workspace",
        )

    if event.created_by_user_id == ctx.user_id:
        return

    # TODO(h-new-1-admin): workspace-admin override. The cloud's
    # auth/domain.py models workspace membership with role ∈ {owner,
    # admin, member, viewer}, but RequestContext in ee/calendar only
    # carries (workspace_id, user_id) today. Wiring the role through
    # requires either (a) extending RequestContext with a role/permissions
    # field populated by the router's _ctx dep, or (b) a one-shot lookup
    # against the workspace membership store from within this function.
    # Option (b) keeps the change local to ee/calendar but adds a DB
    # round-trip per modify call; option (a) is cleaner but touches the
    # router + every test that builds a RequestContext. Tracked separately
    # so this H-NEW-1 fix doesn't block on the broader RBAC plumbing.

    raise Forbidden(
        "event.modify_denied",
        "Only the event creator can modify or delete this event",
    )
