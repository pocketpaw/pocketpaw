"""Cycles domain — FastAPI router.

Thin shell over ``ee.cloud.cycles.service``: parses requests, delegates,
returns DTOs. License gate + canonical ``request_context`` dependency on
every route — services never see raw ``Request`` objects.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.cycles import service as cycles_service
from pocketpaw_ee.cloud.cycles.dto import (
    CreateCycleRequest,
    CycleDailyPointResponse,
    CycleListItemResponse,
    CycleResponse,
    UpdateCycleRequest,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(prefix="/cycles", tags=["Cycles"], dependencies=[Depends(require_license)])


@router.post("", response_model=CycleResponse)
async def create_cycle(
    body: CreateCycleRequest,
    ctx: RequestContext = Depends(request_context),
) -> CycleResponse:
    return await cycles_service.agent_create_cycle(ctx, body)


@router.get("", response_model=list[CycleListItemResponse])
async def list_cycles(
    project_id: str | None = Query(default=None),
    ctx: RequestContext = Depends(request_context),
) -> list[CycleListItemResponse]:
    return await cycles_service.agent_list_cycles(ctx, project_id=project_id)


@router.get("/{cycle_id}", response_model=CycleResponse)
async def get_cycle(
    cycle_id: str,
    ctx: RequestContext = Depends(request_context),
) -> CycleResponse:
    return await cycles_service.agent_get_cycle(ctx, cycle_id)


@router.patch("/{cycle_id}", response_model=CycleResponse)
async def update_cycle(
    cycle_id: str,
    body: UpdateCycleRequest,
    ctx: RequestContext = Depends(request_context),
) -> CycleResponse:
    return await cycles_service.agent_update_cycle(ctx, cycle_id, body)


@router.post("/{cycle_id}/close", response_model=CycleResponse)
async def close_cycle(
    cycle_id: str,
    ctx: RequestContext = Depends(request_context),
) -> CycleResponse:
    return await cycles_service.agent_close_cycle(ctx, cycle_id)


@router.get("/{cycle_id}/items")
async def list_cycle_items(
    cycle_id: str,
    ctx: RequestContext = Depends(request_context),
) -> list[Any]:
    """Return tasks attached to this cycle.

    Falls back to ``[]`` when the Tasks entity (PR 2 of the Mission Control
    series) isn't merged into this branch's ``ee`` snapshot. The wire shape
    matches whatever ``ee.cloud.tasks.service.agent_list_tasks`` returns,
    so the frontend's existing TaskResponse handling passes through
    unchanged once PR 2 ships.
    """
    return await cycles_service.agent_list_cycle_items(ctx, cycle_id)


@router.post("/{cycle_id}/snapshot", response_model=CycleDailyPointResponse | None)
async def snapshot_cycle(
    cycle_id: str,
    ctx: RequestContext = Depends(request_context),
) -> CycleDailyPointResponse | None:
    """Manually trigger today's snapshot for one cycle.

    Useful for tests + intern-onboarding ("force a snapshot now to see
    the chart update"). Idempotent: if today's point already exists, the
    response body is ``null`` and no new point is appended. Returns the
    newly-appended point on success.

    Returns 404 when the cycle doesn't exist in the caller's workspace
    (the tenancy guard inside ``_snapshot_cycle_daily`` raises NotFound,
    which ``_core.http`` maps to the JSON envelope).
    """
    point = await cycles_service._snapshot_cycle_daily(ctx, cycle_id)
    # _snapshot_cycle_daily returns None for "already snapshotted today"
    # OR for "tasks unavailable". The frontend treats both as benign no-ops.
    if point is None:
        # Verify the cycle existed (else _fetch_in_workspace would have
        # raised NotFound). Re-fetch only to surface a clearer 404 on a
        # missing/cross-tenant id; the cycle does exist if we got here.
        return None
    return point


__all__ = ["router"]
