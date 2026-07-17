# router.py — Thin FastAPI adapter for the Code Mode durable-project registry (CM-2a).
# Created 2026-07-16 (feat/code-mode): workspace+user scoped /api/v1/codeproject.
# Tenancy comes from the RequestContext (never the body or query), the service /
# lifecycle do the work, and CloudError -> JSON is handled by _core.http — this
# router never raises HTTPException.
#
# Three routes back the redesigned ``/code`` surface:
#   POST   /codeproject          — create (or return) a durable project for a repo.
#   GET    /codeproject          — the caller's projects grid (most-recent first).
#   POST   /codeproject/{id}/open — resolve the project to a READY sandbox to connect
#                                   to (reuse the bound one or provision a fresh one).
#   PATCH  /codeproject/{id}      — rename a project's display name (owner-scoped).
#   DELETE /codeproject/{id}      — delete a project + tear down its bound VM.
# Like the WebSandbox router, endpoints are license-gated + context-authenticated;
# the tenancy/owner filtering in the service is the security boundary.
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.codeproject import lifecycle as codeproject_lifecycle
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codeproject.dto import (
    CodeProjectListResponse,
    CodeProjectResponse,
    CreateProjectRequest,
    RenameProjectRequest,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.dto import WebSandboxResponse

router = APIRouter(
    prefix="/codeproject",
    tags=["CodeProject"],
    dependencies=[Depends(require_license)],
)


def _require_workspace(ctx: RequestContext) -> str:
    """A workspace-scoped route needs an active workspace; fail closed if absent."""
    if not ctx.workspace_id:
        raise Forbidden("codeproject.no_workspace", "No active workspace")
    return ctx.workspace_id


@router.post("", response_model=CodeProjectResponse)
async def create_project(
    body: CreateProjectRequest,
    ctx: RequestContext = Depends(request_context),
) -> CodeProjectResponse:
    """Create (or return) the durable project for a repo. Idempotent per repo."""
    workspace_id = _require_workspace(ctx)
    view = await codeproject_service.create_project(workspace_id, ctx.user_id, body)
    return codeproject_service.view_to_wire(view)


@router.get("", response_model=CodeProjectListResponse)
async def list_projects(
    ctx: RequestContext = Depends(request_context),
) -> CodeProjectListResponse:
    """List the caller's projects, most-recently-opened first (the projects grid)."""
    workspace_id = _require_workspace(ctx)
    views = await codeproject_service.list_projects(workspace_id, ctx.user_id)
    return CodeProjectListResponse(
        items=[codeproject_service.view_to_wire(v) for v in views]
    )


@router.post("/{project_id}/open", response_model=WebSandboxResponse)
async def open_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> WebSandboxResponse:
    """Open a project → a READY sandbox to connect to (reuse or provision fresh)."""
    workspace_id = _require_workspace(ctx)
    sandbox = await codeproject_lifecycle.open_project(
        workspace_id, ctx.user_id, project_id
    )
    return websandbox_service.view_to_wire(sandbox)


@router.patch("/{project_id}", response_model=CodeProjectResponse)
async def rename_project(
    project_id: str,
    body: RenameProjectRequest,
    ctx: RequestContext = Depends(request_context),
) -> CodeProjectResponse:
    """Rename a project's display name (owner-scoped)."""
    workspace_id = _require_workspace(ctx)
    view = await codeproject_service.rename_project(
        workspace_id, ctx.user_id, project_id, body
    )
    return codeproject_service.view_to_wire(view)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Delete a project and tear down its bound sandbox VM (owner-scoped)."""
    workspace_id = _require_workspace(ctx)
    await codeproject_lifecycle.delete_project(workspace_id, ctx.user_id, project_id)
    return Response(status_code=204)
