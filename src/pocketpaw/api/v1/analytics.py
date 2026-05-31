# Analytics API router — aggregated cost/performance/usage/health views.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from pocketpaw.analytics import (
    get_all_analytics,
    get_cost_analytics,
    get_health_analytics,
    get_performance_analytics,
    get_usage_analytics,
)
from pocketpaw.api.deps import require_scope

router = APIRouter(tags=["Analytics"])


@router.get("/analytics/cost", dependencies=[Depends(require_scope("metrics", "admin"))])
async def analytics_cost(
    period: str = Query("day", pattern="^(day|week|month)$"),
):
    """Return cost and spend trend analytics."""
    try:
        return await get_cost_analytics(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/performance", dependencies=[Depends(require_scope("metrics", "admin"))])
async def analytics_performance(
    period: str = Query("day", pattern="^(day|week|month)$"),
):
    """Return latency/tool performance analytics."""
    try:
        return await get_performance_analytics(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/usage", dependencies=[Depends(require_scope("metrics", "admin"))])
async def analytics_usage(
    period: str = Query("day", pattern="^(day|week|month)$"),
):
    """Return message/session/tool/token usage analytics."""
    try:
        return await get_usage_analytics(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analytics/health", dependencies=[Depends(require_scope("metrics", "admin"))])
async def analytics_health():
    """Return operational health analytics derived from health + traces."""
    return await get_health_analytics()


@router.get("/analytics", dependencies=[Depends(require_scope("metrics", "admin"))])
async def analytics_all(
    period: str = Query("day", pattern="^(day|week|month)$"),
):
    """Return all four analytics views in one pass (single trace scan).

    Preferred endpoint for dashboard full-refresh — use individual endpoints
    only when you need a specific view.
    """
    try:
        return await get_all_analytics(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
