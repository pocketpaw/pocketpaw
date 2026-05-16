# router.py — FastAPI router for the Tasks entity.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Thin
#   pass-through layer. Routers parse requests, delegate to
#   ``ee.cloud.tasks.service``, and return TaskResponse DTOs. No
#   ``HTTPException`` here — services raise CloudError subclasses and
#   ``_core.http`` maps them to JSON.
"""Tasks REST router.

Endpoints:

  - ``POST   /tasks``                  — create
  - ``GET    /tasks``                  — list (workspace-scoped, filterable)
  - ``GET    /tasks/{id}``             — single fetch
  - ``PATCH  /tasks/{id}``             — partial update
  - ``POST   /tasks/{id}/claim``       — agent claims a proposed task
  - ``POST   /tasks/{id}/complete``    — finish (archive or request approval)
  - ``POST   /tasks/{id}/block``       — pause with a reason (Snag)
  - ``POST   /tasks/{id}/reassign``    — handoff to another assignee
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.license import require_license
from ee.cloud.tasks import service as tasks_service
from ee.cloud.tasks.dto import (
    BlockTaskRequest,
    ClaimTaskRequest,
    CompleteTaskRequest,
    CreateTaskRequest,
    ListTasksRequest,
    ReassignTaskRequest,
    TaskResponse,
    UpdateTaskRequest,
)

router = APIRouter(prefix="/tasks", tags=["Tasks"], dependencies=[Depends(require_license)])


@router.post("", response_model=TaskResponse)
async def create_task(
    body: CreateTaskRequest,
    ctx: RequestContext = Depends(request_context),
) -> TaskResponse:
    return await tasks_service.agent_create_task(ctx, body)


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    assignee_id: str | None = Query(default=None, alias="assignee"),
    assignee_kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    cycle_id: str | None = Query(default=None),
    pocket_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    creator_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[TaskResponse]:
    body = ListTasksRequest(
        assignee_id=assignee_id,
        assignee_kind=assignee_kind,  # type: ignore[arg-type]
        status=status,
        cycle_id=cycle_id,
        pocket_id=pocket_id,
        project_id=project_id,
        creator_id=creator_id,
        limit=limit,
    )
    return await tasks_service.agent_list_tasks(ctx, body)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    ctx: RequestContext = Depends(request_context),
) -> TaskResponse:
    return await tasks_service.agent_get_task(ctx, task_id)


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str,
    body: UpdateTaskRequest,
    ctx: RequestContext = Depends(request_context),
) -> TaskResponse:
    return await tasks_service.agent_update_task(ctx, task_id, body)


@router.post("/{task_id}/claim")
async def claim_task(
    task_id: str,
    body: ClaimTaskRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Atomic claim. Returns ``{ok: True, task}`` on success,
    ``{ok: False, reason}`` when the row was already claimed, doesn't
    exist, or isn't assigned to the calling agent. The 200 status with
    ``ok=False`` is intentional — agent runtimes treat this as a polling
    race, not an exceptional error, and the body carries the reason."""

    return await tasks_service.agent_claim_task(ctx, task_id, body)


@router.post("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(
    task_id: str,
    body: CompleteTaskRequest,
    ctx: RequestContext = Depends(request_context),
) -> TaskResponse:
    return await tasks_service.agent_complete_task(ctx, task_id, body)


@router.post("/{task_id}/block", response_model=TaskResponse)
async def block_task(
    task_id: str,
    body: BlockTaskRequest,
    ctx: RequestContext = Depends(request_context),
) -> TaskResponse:
    return await tasks_service.agent_block_task(ctx, task_id, body)


@router.post("/{task_id}/reassign", response_model=TaskResponse)
async def reassign_task(
    task_id: str,
    body: ReassignTaskRequest,
    ctx: RequestContext = Depends(request_context),
) -> TaskResponse:
    return await tasks_service.agent_reassign_task(ctx, task_id, body)
