# Calendar module — service layer (module-level async functions).
# Created: 2026-05-19 (feat/calendar-module).
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

from beanie import PydanticObjectId

from ee.calendar._context import RequestContext
from ee.calendar.conflicts import _doc_to_event, find_conflicts
from ee.calendar.domain import ConflictSeverity, Event
from ee.calendar.dto import (
    ConflictReport,
    CreateEventRequest,
    EventListResponse,
    EventResponse,
    FreeBusyRequest,
    FreeBusyResponse,
    ListEventsRequest,
    UpdateEventRequest,
)
from ee.calendar.events import (
    TOPIC_CONFLICT_DETECTED,
    TOPIC_EVENT_CREATED,
    TOPIC_EVENT_DELETED,
    TOPIC_EVENT_UPDATED,
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    ConflictDetected,
)
from ee.calendar.freebusy import compute_freebusy
from ee.calendar.models import _EventDoc
from ee.cloud.shared.errors import NotFound, ValidationError
from ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _event_to_response(event: Event) -> EventResponse:
    """Mapping via Pydantic — never hand-rolled."""
    return EventResponse.model_validate(event.model_dump())


def _doc_to_response(doc: _EventDoc) -> EventResponse:
    return _event_to_response(_doc_to_event(doc))


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
    """Fetch a single event. Tenant-filtered."""
    doc = await _get_event_doc_or_404(ctx, event_id)
    return _doc_to_response(doc)


async def update_event(
    ctx: RequestContext,
    event_id: str,
    body: UpdateEventRequest,
) -> EventResponse:
    """Patch an event. At least one field must be set (DTO enforces)."""
    body = UpdateEventRequest.model_validate(body)
    doc = await _get_event_doc_or_404(ctx, event_id)

    changes: dict = {}
    if body.title is not None:
        doc.title = body.title
        changes["title"] = body.title
    if body.description is not None:
        doc.description = body.description
        changes["description"] = body.description
    if body.starts_at is not None:
        doc.starts_at = body.starts_at
        changes["starts_at"] = body.starts_at.isoformat()
    if body.ends_at is not None:
        doc.ends_at = body.ends_at
        changes["ends_at"] = body.ends_at.isoformat()
    if body.location is not None:
        doc.location = body.location
        changes["location"] = body.location
    if body.attendees is not None:
        doc.attendees = [a.model_dump() for a in body.attendees]
        changes["attendees"] = len(body.attendees)
    if body.recurrence is not None:
        doc.recurrence = body.recurrence.model_dump()
        changes["recurrence"] = True

    # Cross-field validation after the partial merge — starts_at < ends_at.
    if doc.ends_at <= doc.starts_at:
        raise ValidationError(
            "event.invalid_window",
            "ends_at must be strictly after starts_at",
        )

    doc.updated_at = datetime.now(UTC)
    await doc.save()

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
    """Hard-delete an event. Tenant-filtered."""
    doc = await _get_event_doc_or_404(ctx, event_id)
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
    """List events in a time window. Tenant-filtered."""
    body = ListEventsRequest.model_validate(body)

    query: dict = {
        "workspace": ctx.workspace_id,
        "starts_at": {"$lt": body.starts_before},
        "ends_at": {"$gt": body.starts_after},
    }
    if body.calendar_id is not None:
        query["calendar_id"] = body.calendar_id

    docs = await _EventDoc.find(query).limit(body.limit).to_list()
    events = [_doc_to_response(d) for d in docs]
    return EventListResponse(events=events, total=len(events))


async def get_freebusy(ctx: RequestContext, body: FreeBusyRequest) -> FreeBusyResponse:
    """Compute per-attendee busy periods. Tenant-filtered inside compute_freebusy."""
    body = FreeBusyRequest.model_validate(body)
    freebusy = await compute_freebusy(
        workspace_id=ctx.workspace_id,
        attendee_emails=body.attendee_emails,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
    )
    return FreeBusyResponse(freebusy=freebusy)


async def detect_conflicts(ctx: RequestContext, event_id: str) -> ConflictReport:
    """Find conflicts for an event. Emits on conflict so future approval
    flows can hook in."""
    doc = await _get_event_doc_or_404(ctx, event_id)
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
