# Created: 2026-05-17 — pocketpaw#1118 P1. FastAPI router for the
#   planner entity. Thin pass-through: parse request → delegate to
#   ``planner.service`` → return DTO. No ``HTTPException`` here —
#   services raise CloudError subclasses and ``_core.http`` maps them
#   to JSON.
"""Planner REST router.

Endpoints:

  - ``POST /planner/run``                       — invoke planner for a
    cloud Project; returns ``PlanProjectResult``.
  - ``GET  /planner/by-project/{project_id}``   — fetch the most recent
    plan summary for ``project_id``; ``204`` when no plan exists yet.

Mounted at ``/api/v1/planner`` in ``ee.cloud.__init__.mount_cloud``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.license import require_license
from ee.cloud.planner import service as planner_service
from ee.cloud.planner.dto import PlanProjectRequest, PlanProjectResult

router = APIRouter(
    prefix="/planner",
    tags=["Planner"],
    dependencies=[Depends(require_license)],
)


@router.post("/run", response_model=PlanProjectResult)
async def run_planner(
    body: PlanProjectRequest,
    ctx: RequestContext = Depends(request_context),
) -> PlanProjectResult:
    """Invoke the OSS deep_work planner for a cloud Project.

    Long-running — the OSS planner makes 3-4 LLM calls. Clients should
    surface a spinner while this is in-flight; deep_research=True can
    push the wall-clock past a minute.
    """
    return await planner_service.agent_plan_project(ctx, body)


@router.get("/by-project/{project_id}", response_model=PlanProjectResult)
async def get_plan_by_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> PlanProjectResult | Response:
    """Return the most recent plan summary for ``project_id``.

    Returns ``204 No Content`` when no plan has been generated yet —
    the FE Plan tab interprets that as "show the Generate plan CTA"
    rather than rendering an empty summary card.
    """
    result = await planner_service.get_plan_for_project(ctx, project_id)
    if result is None:
        return Response(status_code=204)
    return result
