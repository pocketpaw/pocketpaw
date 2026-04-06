# Plan mode router — approve/reject plans.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from pocketpaw.api.v1.schemas.plan_mode import PlanActionRequest, PlanActionResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Plan Mode"])


@router.post("/plan/approve", response_model=PlanActionResponse)
async def approve_plan(body: PlanActionRequest):
    """Approve a proposed execution plan."""
    from pocketpaw.agents.plan_mode import get_plan_manager

    pm = get_plan_manager()
    plan = pm.approve_plan(body.session_key)

    if plan is None:
        raise HTTPException(status_code=404, detail="No active plan to approve")

    return PlanActionResponse(session_key=body.session_key, action="approved")


@router.post("/plan/reject", response_model=PlanActionResponse)
async def reject_plan(body: PlanActionRequest):
    """Reject a proposed execution plan."""
    from pocketpaw.agents.plan_mode import get_plan_manager

    pm = get_plan_manager()
    plan = pm.reject_plan(body.session_key)

    if plan is None:
        raise HTTPException(status_code=404, detail="No active plan to reject")

    return PlanActionResponse(session_key=body.session_key, action="rejected")
