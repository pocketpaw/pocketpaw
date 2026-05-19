# Calendar module — FastAPI router.
# Created: 2026-05-19 (feat/calendar-module).
#
# Thin wrappers around service.py. No business logic here. The router is
# the single mounting point — callers outside this module never touch
# service.py directly. Errors raised by service propagate to the global
# CloudError handler installed by ee.cloud (we never raise HTTPException).
#
# This PR does NOT mount the router into the cloud app. A separate PR will
# wire it in (so we can roll out behind a feature flag).

from __future__ import annotations

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
    starts_after: str,
    starts_before: str,
    calendar_id: str | None = None,
    limit: int = 100,
    ctx: RequestContext = Depends(_ctx),
) -> EventListResponse:
    from datetime import datetime

    body = ListEventsRequest(
        calendar_id=calendar_id,
        starts_after=datetime.fromisoformat(starts_after),
        starts_before=datetime.fromisoformat(starts_before),
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
