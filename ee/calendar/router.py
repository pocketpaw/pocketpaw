# Calendar module — FastAPI router.
# Updated: 2026-05-19 (fix/calendar-security-hardening, #1142 H3).
#
# Changes (H3):
# - ``list_events`` now declares ``starts_after`` and ``starts_before`` as
#   ``datetime`` query parameters directly. FastAPI parses them via
#   Pydantic and returns a 422 on malformed input. Previously we received
#   raw ``str`` and called ``datetime.fromisoformat`` unguarded, which
#   bubbled a ``ValueError`` into the global handler as HTTP 500.
#
# Thin wrappers around service.py. No business logic here. The router is
# the single mounting point — callers outside this module never touch
# service.py directly. Errors raised by service propagate to the global
# CloudError handler installed by ee.cloud (we never raise HTTPException).

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.calendar._context import RequestContext
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
from ee.calendar.service import (
    create_event as svc_create_event,
)
from ee.calendar.service import (
    delete_event as svc_delete_event,
)
from ee.calendar.service import (
    detect_conflicts as svc_detect_conflicts,
)
from ee.calendar.service import (
    get_event as svc_get_event,
)
from ee.calendar.service import (
    get_freebusy as svc_get_freebusy,
)
from ee.calendar.service import (
    list_events as svc_list_events,
)
from ee.calendar.service import (
    update_event as svc_update_event,
)
from ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(prefix="/api/v1/calendar", tags=["Calendar"])


async def _ctx(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> RequestContext:
    """Assemble the RequestContext from the standard cloud auth deps."""
    return RequestContext(workspace_id=workspace_id, user_id=user_id)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@router.post("/events", response_model=EventResponse, status_code=201)
async def create_event(
    body: CreateEventRequest,
    ctx: RequestContext = Depends(_ctx),
) -> EventResponse:
    return await svc_create_event(ctx, body)


@router.get("/events", response_model=EventListResponse)
async def list_events(
    starts_after: datetime,
    starts_before: datetime,
    calendar_id: str | None = None,
    limit: int = 100,
    ctx: RequestContext = Depends(_ctx),
) -> EventListResponse:
    # H3: starts_after / starts_before are now declared as datetime — FastAPI
    # parses them via Pydantic and returns 422 on malformed input. No more
    # try/except around datetime.fromisoformat.
    body = ListEventsRequest(
        calendar_id=calendar_id,
        starts_after=starts_after,
        starts_before=starts_before,
        limit=limit,
    )
    return await svc_list_events(ctx, body)


@router.get("/events/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: str,
    ctx: RequestContext = Depends(_ctx),
) -> EventResponse:
    return await svc_get_event(ctx, event_id)


@router.patch("/events/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: str,
    body: UpdateEventRequest,
    ctx: RequestContext = Depends(_ctx),
) -> EventResponse:
    return await svc_update_event(ctx, event_id, body)


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(
    event_id: str,
    ctx: RequestContext = Depends(_ctx),
) -> Response:
    await svc_delete_event(ctx, event_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# FreeBusy
# ---------------------------------------------------------------------------


@router.post("/freebusy", response_model=FreeBusyResponse)
async def get_freebusy(
    body: FreeBusyRequest,
    ctx: RequestContext = Depends(_ctx),
) -> FreeBusyResponse:
    return await svc_get_freebusy(ctx, body)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


@router.get("/events/{event_id}/conflicts", response_model=ConflictReport)
async def detect_conflicts(
    event_id: str,
    ctx: RequestContext = Depends(_ctx),
) -> ConflictReport:
    return await svc_detect_conflicts(ctx, event_id)
