# router.py — FastAPI router for the Projects entity.
# Created: 2026-05-16 — Mission Control backend completion. Thin
#   pass-through layer. Routers parse requests, delegate to
#   ``ee.cloud.projects.service``, and return ProjectResponse DTOs. No
#   ``HTTPException`` here — services raise CloudError subclasses and
#   ``_core.http`` maps them to JSON.
"""Projects REST router.

Endpoints:

  - ``POST   /projects``                — create
  - ``GET    /projects``                — list (workspace-scoped, filterable)
  - ``GET    /projects/{id}``           — single fetch
  - ``PATCH  /projects/{id}``           — partial update
  - ``POST   /projects/{id}/archive``   — soft-archive
  - ``DELETE /projects/{id}``           — hard-delete + cascade unassign
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from starlette.responses import Response

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.license import require_license
from ee.cloud.projects import service as projects_service
from ee.cloud.projects.dto import (
    CreateProjectRequest,
    ListProjectsRequest,
    ProjectResponse,
    UpdateProjectRequest,
)

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
    dependencies=[Depends(require_license)],
)


@router.post("", response_model=ProjectResponse)
async def create_project(
    body: CreateProjectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProjectResponse:
    return await projects_service.agent_create(ctx, body)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[ProjectResponse]:
    body = ListProjectsRequest(status=status, limit=limit)  # type: ignore[arg-type]
    return await projects_service.agent_list(ctx, body)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProjectResponse:
    return await projects_service.agent_get(ctx, project_id)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProjectResponse:
    return await projects_service.agent_update(ctx, project_id, body)


@router.post("/{project_id}/archive", response_model=ProjectResponse)
async def archive_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProjectResponse:
    return await projects_service.agent_archive(ctx, project_id)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    await projects_service.agent_delete(ctx, project_id)
    return Response(status_code=204)
