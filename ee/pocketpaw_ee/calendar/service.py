# Calendar module — service layer (module-level async functions).
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H1/H2/M4 + H-NEW-1).
#
# Changes:
# - H1: Every CRUD path now resolves the parent Calendar and invokes
#   ``policy.check_calendar_read`` / ``policy.check_calendar_write`` before
#   touching the event. The policy module was previously dead code; this
#   wires it as the authz chokepoint.
# - H2: ``get_freebusy`` pre-validates the requested attendee list and
#   passes ``accessible_calendar_ids`` into ``compute_freebusy`` so the
#   result only contains busy windows the caller could read directly.
#   Unknown attendees → ``ValidationError`` (was: silent oracle).
# - M4: ``update_event`` no longer puts raw ``title``/``description``/
#   ``location``/``starts_at``/``ends_at`` content into the
#   ``CalendarEventUpdated`` bus payload. Bus subscribers receive a
#   ``changed_fields`` list (field names only) instead.
# - H-NEW-1: ``create_event`` now stamps ``created_by_user_id`` from
#   ``ctx.user_id`` on the new ``_EventDoc``. ``update_event`` and
#   ``delete_event`` call ``policy.check_event_modify(ctx, event)`` after
#   ``check_calendar_write`` so the synthetic-default Calendar path (used
#   until Calendar CRUD ships) can't be exploited to mutate other
#   members' events. ``_doc_to_event`` (in conflicts.py) propagates the
#   field into the domain Event so the modify check has the data it
#   needs.
# - Helper ``_load_calendar`` falls back to a synthesized default
#   Calendar when no ``_CalendarDoc`` row exists yet. The Calendar CRUD
#   surface ships in a follow-up; until then policy still enforces the
#   tenant boundary and lets the event creator (= caller's workspace)
#   proceed by default. Override the default by inserting a
#   ``_CalendarDoc`` with the desired ``visibility``/``owner_user_id``.
#
# All write paths through here. _EventDoc / _CalendarDoc are never
# imported outside this file. Every function:
#   1. Validates the request DTO at entry (model_validate).
#   2. Reads/writes with workspace=ctx.workspace_id as the tenant filter.
#   3. Raises CloudError subclasses (NotFound, Forbidden, ValidationError) —
#      never HTTPException.
#   4. Emits on event_bus for state-changing ops so subscribers (notifications,
#      future Instinct approval gates, sync) can react without coupling.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.calendar import policy
from pocketpaw_ee.calendar._context import RequestContext
from pocketpaw_ee.calendar.conflicts import _doc_to_event, find_conflicts
from pocketpaw_ee.calendar.domain import Calendar, CalendarVisibility, ConflictSeverity, Event
from pocketpaw_ee.calendar.dto import (
    ConflictReport,
    CreateEventRequest,
    EventListResponse,
    EventResponse,
    FreeBusyRequest,
    FreeBusyResponse,
    ListEventsRequest,
    UpdateEventRequest,
)
from pocketpaw_ee.calendar.events import (
    TOPIC_CONFLICT_DETECTED,
    TOPIC_EVENT_CREATED,
    TOPIC_EVENT_DELETED,
    TOPIC_EVENT_UPDATED,
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    ConflictDetected,
)
from pocketpaw_ee.calendar.freebusy import compute_freebusy
from pocketpaw_ee.calendar.models import _CalendarDoc, _EventDoc
from pocketpaw_ee.calendar.recurrence import expand_recurrence
from pocketpaw_ee.cloud.shared.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _event_to_response(event: Event) -> EventResponse:
    """Mapping via Pydantic — never hand-rolled."""
    return EventResponse.model_validate(event.model_dump())


def _doc_to_response(doc: _EventDoc) -> EventResponse:
    return _event_to_response(_doc_to_event(doc))


def _doc_to_calendar(doc: _CalendarDoc) -> Calendar:
    """Map a Beanie ``_CalendarDoc`` to a domain ``Calendar``.

    The DB schema uses string-typed visibility; coerce to the StrEnum so
    policy can pattern-match on the enum value rather than a raw string.
    """
    try:
        vis = CalendarVisibility(doc.visibility)
    except ValueError:
        # Defensive fallback — should never happen for rows written by this
        # service, but a hand-edit could put a garbage string in. Treat as
        # the most restrictive option.
        vis = CalendarVisibility.PRIVATE
    return Calendar(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        owner_user_id=doc.owner_user_id,
        timezone=doc.timezone,
        color=doc.color,
        visibility=vis,
        shared_with_user_ids=list(doc.shared_with_user_ids or []),
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def _load_calendar(ctx: RequestContext, calendar_id: str) -> Calendar:
    """Resolve a ``Calendar`` domain object for ``calendar_id``.

    If a ``_CalendarDoc`` exists in the caller's workspace, it's returned.
    Otherwise a synthetic default Calendar is returned (workspace-public,
    owned by the caller) so policy still runs and the tenant boundary
    holds. The synthetic path is the bridge until the Calendar CRUD
    surface lands; once Calendar rows are required, this falls back to
    raising NotFound.
    """
    doc = await _CalendarDoc.find_one(
        {"_id": _maybe_object_id(calendar_id), "workspace": ctx.workspace_id}
    )
    if doc is not None:
        return _doc_to_calendar(doc)
    # No row yet — synthesize a default so policy still enforces the
    # tenant boundary. Owner = caller so subsequent writes by the caller
    # still succeed; visibility = workspace-public so reads in-workspace
    # still succeed. This matches the pre-#1142 behaviour where the only
    # check was the tenant filter.
    now = datetime.now(UTC)
    return Calendar(
        id=calendar_id,
        workspace_id=ctx.workspace_id,
        name=calendar_id,
        owner_user_id=ctx.user_id,
        timezone="UTC",
        visibility=CalendarVisibility.PUBLIC_TO_WORKSPACE,
        shared_with_user_ids=[],
        created_at=now,
        updated_at=now,
    )


def _maybe_object_id(value: str) -> Any:
    """Try to coerce ``value`` to ``PydanticObjectId``; on failure return the
    raw string so Beanie can still match on opaque external ids (rare,
    but supported for calendars synced from external systems)."""
    try:
        return PydanticObjectId(value)
    except Exception:  # noqa: BLE001 — Beanie / bson raise a variety of types
        return value


async def _get_event_doc_or_404(ctx: RequestContext, event_id: str) -> _EventDoc:
    """Tenant-filtered fetch. Cross-workspace lookups return 404, not 403 —
    we never reveal existence outside the tenant."""
    try:
        oid = PydanticObjectId(event_id)
    except Exception as exc:  # noqa: BLE001 — Beanie raises a variety of types here
        raise NotFound("event", event_id) from exc

    doc = await _EventDoc.find_one(
        _EventDoc.id == oid,
        _EventDoc.workspace == ctx.workspace_id,
    )
    if not doc:
        raise NotFound("event", event_id)
    return doc


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_event(ctx: RequestContext, body: CreateEventRequest) -> EventResponse:
    """Create a calendar event."""
    body = CreateEventRequest.model_validate(body)

    # H1: enforce write access on the parent calendar before any insert.
    calendar = await _load_calendar(ctx, body.calendar_id)
    policy.check_calendar_write(ctx, calendar)

    # An optional Instinct approval gate hooks here in a follow-up PR — for
    # now the comment marks the seam so reviewers know where it lands.
    # TODO(instinct): if ctx.workspace requires approval for calendar
    # writes, route through ee.instinct.propose() instead of inserting.

    doc = _EventDoc(
        workspace=ctx.workspace_id,
        calendar_id=body.calendar_id,
        title=body.title,
        description=body.description,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        timezone=body.timezone,
        # H-NEW-1: stamp the creator from the request context. Never accept
        # this from the client — the DTO doesn't expose it for that reason.
        created_by_user_id=ctx.user_id,
        location=body.location,
        attendees=[a.model_dump() for a in body.attendees],
        recurrence=body.recurrence.model_dump() if body.recurrence else None,
        fabric_object_id=body.fabric_object_id,
    )
    await doc.insert()

    await event_bus.emit(
        TOPIC_EVENT_CREATED,
        CalendarEventCreated(
            event_id=str(doc.id),
            workspace_id=ctx.workspace_id,
            calendar_id=doc.calendar_id,
            starts_at=doc.starts_at,
            ends_at=doc.ends_at,
        ).model_dump(),
    )

    return _doc_to_response(doc)


async def get_event(ctx: RequestContext, event_id: str) -> EventResponse:
    """Fetch a single event. Tenant-filtered + policy-gated."""
    doc = await _get_event_doc_or_404(ctx, event_id)
    calendar = await _load_calendar(ctx, doc.calendar_id)
    # H1: read gate. Tenant filter already passed; this enforces
    # within-workspace authz (private/shared calendars).
    policy.check_calendar_read(ctx, calendar)
    return _doc_to_response(doc)


async def update_event(
    ctx: RequestContext,
    event_id: str,
    body: UpdateEventRequest,
) -> EventResponse:
    """Patch an event. At least one field must be set (DTO enforces)."""
    body = UpdateEventRequest.model_validate(body)
    doc = await _get_event_doc_or_404(ctx, event_id)

    # H1: enforce write access on the event's calendar.
    calendar = await _load_calendar(ctx, doc.calendar_id)
    policy.check_calendar_write(ctx, calendar)

    # H-NEW-1: calendar-level write isn't enough on a synthetic-default
    # Calendar, where check_calendar_write trivially passes for every
    # workspace member (because the synthetic owner = ctx.user_id). The
    # event-level gate ensures only the creator (or, later, a workspace
    # admin) can edit an existing event.
    policy.check_event_modify(ctx, _doc_to_event(doc))

    # M4: track only the names of fields that changed, NOT the values.
    # Bus subscribers (notifications, search reindex, soul memory) only
    # need to know WHAT changed to invalidate their caches — leaking the
    # raw title/description content into the in-process bus widened the
    # blast radius for any future handler that logs payloads.
    changed_fields: list[str] = []
    if body.title is not None:
        doc.title = body.title
        changed_fields.append("title")
    if body.description is not None:
        doc.description = body.description
        changed_fields.append("description")
    if body.starts_at is not None:
        doc.starts_at = body.starts_at
        changed_fields.append("starts_at")
    if body.ends_at is not None:
        doc.ends_at = body.ends_at
        changed_fields.append("ends_at")
    if body.location is not None:
        doc.location = body.location
        changed_fields.append("location")
    if body.attendees is not None:
        doc.attendees = [a.model_dump() for a in body.attendees]
        changed_fields.append("attendees")
    if "recurrence" in body.model_fields_set:
        doc.recurrence = body.recurrence.model_dump() if body.recurrence is not None else None
        changed_fields.append("recurrence")

    # Cross-field validation after the partial merge — starts_at < ends_at.
    if doc.ends_at <= doc.starts_at:
        raise ValidationError(
            "event.invalid_window",
            "ends_at must be strictly after starts_at",
        )

    doc.updated_at = datetime.now(UTC)
    await doc.save()

    # M4: payload now carries only field names. Attendees still carries a
    # count (no PII) for consumers that wanted "did the attendee list
    # change in shape".
    changes: dict[str, Any] = {"changed_fields": changed_fields}
    if body.attendees is not None:
        changes["attendees_count"] = len(body.attendees)

    await event_bus.emit(
        TOPIC_EVENT_UPDATED,
        CalendarEventUpdated(
            event_id=str(doc.id),
            workspace_id=ctx.workspace_id,
            calendar_id=doc.calendar_id,
            changes=changes,
        ).model_dump(),
    )

    return _doc_to_response(doc)


async def delete_event(ctx: RequestContext, event_id: str) -> None:
    """Hard-delete an event. Tenant-filtered + policy-gated."""
    doc = await _get_event_doc_or_404(ctx, event_id)

    # H1: enforce write access before deletion.
    calendar = await _load_calendar(ctx, doc.calendar_id)
    policy.check_calendar_write(ctx, calendar)

    # H-NEW-1: same rationale as update_event — calendar-level write
    # passes trivially on a synthetic-default Calendar, so we add the
    # event-level creator gate before destroying the row.
    policy.check_event_modify(ctx, _doc_to_event(doc))

    event_id_str = str(doc.id)
    calendar_id = doc.calendar_id
    await doc.delete()

    await event_bus.emit(
        TOPIC_EVENT_DELETED,
        CalendarEventDeleted(
            event_id=event_id_str,
            workspace_id=ctx.workspace_id,
            calendar_id=calendar_id,
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def list_events(ctx: RequestContext, body: ListEventsRequest) -> EventListResponse:
    """List events in a time window. Tenant-filtered + per-event policy filter.

    H1: events on calendars the caller can't read are silently dropped
    from the result. When ``calendar_id`` is supplied, the calendar is
    resolved once and a hard read-gate is raised on deny (so the caller
    sees a clear 403 rather than an empty list).
    """
    body = ListEventsRequest.model_validate(body)

    if body.calendar_id is not None:
        calendar = await _load_calendar(ctx, body.calendar_id)
        # Hard gate for the single-calendar case — better UX than silently
        # returning empty.
        policy.check_calendar_read(ctx, calendar)

    query: dict = {
        "workspace": ctx.workspace_id,
        "starts_at": {"$lt": body.starts_before},
        "$or": [
            {"ends_at": {"$gt": body.starts_after}},
            {"recurrence": {"$ne": None}},
        ],
    }
    if body.calendar_id is not None:
        query["calendar_id"] = body.calendar_id

    # Use the max allowed limit on the raw query so recurring events are
    # not silently dropped before expansion. The final result is capped
    # to body.limit after expansion and sort below.
    query_limit = max(body.limit, 500)
    docs = await _EventDoc.find(query).limit(query_limit).to_list()

    # Resolve the set of unique calendar_ids in the result and check
    # read access in one pass. Cache per-calendar decisions so the
    # filter stays O(events).
    accessible: dict[str, bool] = {}
    visible_docs: list[_EventDoc] = []
    for d in docs:
        cid = getattr(d, "calendar_id", None)
        if cid is None:
            continue
        if cid not in accessible:
            cal = await _load_calendar(ctx, cid)
            accessible[cid] = policy.can_read_calendar(ctx, cal)
        if accessible[cid]:
            visible_docs.append(d)

    # Map to domain events and expand recurrences into the query window.
    # The request boundaries are timezone-naive (FastAPI default). Stamp
    # them as UTC so they compare with the timezone-aware datetimes
    # produced by _doc_to_event.
    range_start = body.starts_after.replace(tzinfo=UTC)
    range_end = body.starts_before.replace(tzinfo=UTC)
    expanded: list[Event] = []
    for d in visible_docs:
        event = _doc_to_event(d)
        if event.recurrence is not None:
            expanded.extend(expand_recurrence(event, range_start, range_end))
        elif event.ends_at > range_start and event.starts_at < range_end:
            expanded.append(event)

    # Sort by starts_at and cap to the requested limit.
    expanded.sort(key=lambda e: e.starts_at)
    events = [_event_to_response(e) for e in expanded[: body.limit]]
    return EventListResponse(events=events, total=len(expanded))


async def get_freebusy(ctx: RequestContext, body: FreeBusyRequest) -> FreeBusyResponse:
    """Compute per-attendee busy periods.

    H2: closes the workspace-wide oracle that #1132 flagged. Two layers:
      1. Pre-validate the attendee list — every requested email must
         appear as an attendee on at least one event sitting on a
         calendar the caller can already read. Unknown emails raise
         ``ValidationError`` so a probe attempt isn't ambiguous.
      2. Pass ``accessible_calendar_ids`` into ``compute_freebusy`` so
         that even after pre-validation, events on other calendars don't
         contribute to the busy windows.
    """
    body = FreeBusyRequest.model_validate(body)

    accessible_calendar_ids = await _accessible_calendar_ids_with_email_match(
        ctx,
        attendee_emails=body.attendee_emails,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
    )

    freebusy = await compute_freebusy(
        workspace_id=ctx.workspace_id,
        attendee_emails=body.attendee_emails,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        accessible_calendar_ids=accessible_calendar_ids,
    )
    return FreeBusyResponse(freebusy=freebusy)


async def _accessible_calendar_ids_with_email_match(
    ctx: RequestContext,
    *,
    attendee_emails: list[str],
    starts_at: datetime,
    ends_at: datetime,
) -> set[str]:
    """Compute the set of calendar_ids the caller can read AND that host
    at least one event whose attendee list overlaps the requested emails
    in the window.

    Also enforces the "no unknown emails" half of H2: every requested
    email must appear in the resolved set; otherwise raise
    ``ValidationError``. This prevents using freebusy as a directory
    probe.
    """
    emails_lower = {e.lower() for e in attendee_emails}

    # Find candidate events touching the window with at least one of the
    # requested attendees. We restrict to the caller's workspace from the
    # start — no cross-tenant leak.
    candidate_docs = await _EventDoc.find(
        {
            "workspace": ctx.workspace_id,
            "starts_at": {"$lt": ends_at},
            "ends_at": {"$gt": starts_at},
            "attendees.email": {"$in": list(emails_lower)},
        }
    ).to_list()

    # Bucket emails by the calendars they appear on (for the unknown-email
    # check). Also collect the unique set of calendar_ids we still need
    # to resolve.
    emails_with_match: set[str] = set()
    candidate_calendar_ids: set[str] = set()
    for doc in candidate_docs:
        cid = getattr(doc, "calendar_id", None)
        if cid is None:
            continue
        candidate_calendar_ids.add(cid)
        for att in getattr(doc, "attendees", None) or []:
            email = (att.get("email") or "").lower()
            if email in emails_lower:
                emails_with_match.add(email)

    # Resolve each candidate calendar's read access once.
    accessible: set[str] = set()
    for cid in candidate_calendar_ids:
        cal = await _load_calendar(ctx, cid)
        if policy.can_read_calendar(ctx, cal):
            accessible.add(cid)

    # Unknown-email check — every requested email must be matched to at
    # least one event on a calendar the caller can read. We rebuild
    # ``emails_with_match`` over accessible calendars only.
    visible_emails: set[str] = set()
    for doc in candidate_docs:
        cid = getattr(doc, "calendar_id", None)
        if cid not in accessible:
            continue
        for att in getattr(doc, "attendees", None) or []:
            email = (att.get("email") or "").lower()
            if email in emails_lower:
                visible_emails.add(email)

    missing = emails_lower - visible_emails
    if missing:
        # Don't leak which specific emails were unknown — the response
        # only says one or more attendees aren't reachable from the
        # caller's calendars. This still tells a malicious caller "this
        # query was refused" but never "this email exists in another
        # workspace" or "this email is real but on a private calendar".
        raise ValidationError(
            "calendar.unknown_attendee",
            "One or more attendee emails are not reachable from your accessible calendars",
        )

    return accessible


async def detect_conflicts(ctx: RequestContext, event_id: str) -> ConflictReport:
    """Find conflicts for an event. Emits on conflict so future approval
    flows can hook in."""
    doc = await _get_event_doc_or_404(ctx, event_id)

    # H1: caller must be able to read the calendar this event lives on
    # before we run conflict detection (which scans other events in the
    # workspace).
    calendar = await _load_calendar(ctx, doc.calendar_id)
    policy.check_calendar_read(ctx, calendar)

    target = _doc_to_event(doc)

    conflicts = await find_conflicts(ctx.workspace_id, target)

    severity = _severity_for(len(conflicts))

    if conflicts:
        await event_bus.emit(
            TOPIC_CONFLICT_DETECTED,
            ConflictDetected(
                event_id=event_id,
                workspace_id=ctx.workspace_id,
                conflicting_event_ids=[c.id for c in conflicts],
            ).model_dump(),
        )

    return ConflictReport(
        event_id=event_id,
        conflicting_events=[_event_to_response(c) for c in conflicts],
        severity=severity,
    )


def _severity_for(conflict_count: int) -> ConflictSeverity:
    """Crude severity bucketing — refine when product gives us real rules."""
    if conflict_count == 0:
        return ConflictSeverity.LOW
    if conflict_count < 3:
        return ConflictSeverity.MEDIUM
    return ConflictSeverity.HIGH
