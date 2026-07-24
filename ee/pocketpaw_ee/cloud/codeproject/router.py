# router.py — Thin FastAPI adapter for the Code Mode durable-project registry (CM-2a).
# Created 2026-07-16 (feat/code-mode): workspace+user scoped /api/v1/codeproject.
# Tenancy comes from the RequestContext (never the body or query), the service /
# lifecycle do the work, and CloudError -> JSON is handled by _core.http — this
# router never raises HTTPException.
#
# Three routes back the redesigned ``/code`` surface:
#   POST   /codeproject          — create (or return) a durable project for a repo.
#   GET    /codeproject          — the caller's projects grid (most-recent first).
#   GET    /codeproject/{id}      — read ONE project without opening it. Side-effect
#                                   free, so the client can pick a runtime
#                                   (BrowserPod vs Daytona) without provisioning a VM.
#   POST   /codeproject/{id}/open — resolve the project to a READY sandbox to connect
#                                   to (reuse the bound one or provision a fresh one).
#   PATCH  /codeproject/{id}      — rename a project's display name (owner-scoped).
#   PATCH  /codeproject/{id}/consume-prompt — mark the initial build prompt consumed
#                                   on build-turn start, or re-arm it on a retry.
#   DELETE /codeproject/{id}      — delete a project + tear down its bound VM.
#   PUT    /codeproject/{id}/file  — persist ONE file written by the in-browser
#                                   runtime (write-through to blob storage).
#   DELETE /codeproject/{id}/file  — drop one file so it isn't restored again.
#   GET    /codeproject/{id}/files — the project's whole persisted state, the restore
#                                   payload the in-tab runtime replays.
# Like the WebSandbox router, endpoints are license-gated + context-authenticated;
# the tenancy/owner filtering in the service is the security boundary.
#
# Modified 2026-07-24 (feat/code-initial-prompt): added the consume-prompt PATCH
# route so the frontend can flip ``initial_prompt_consumed`` when it kicks off the
# auto-run build turn (and re-arm it on a retry-build).
#
# Modified 2026-07-25 (B1, feat/code-project-file-sync): added the three file-sync
# routes above. They exist because the in-tab (WebContainer) runtime writes to a
# filesystem inside the browser that no backend hook can see — the Daytona path
# mirrors edits from the ``file.write`` WebSocket verb, but in a tab there is no
# VM to mirror out of, so the client itself has to push each write through and
# pull the whole set back on a reopen. Storage lives in ``websandbox/durability.py``
# (the layer allowed to touch EEUploadService); like the websandbox snapshot /
# restore routes, this router calls it directly and stays a thin adapter.
#
# Modified 2026-07-25 (B4, feat/code-cross-runtime-restore): docs only. The GET
# /files payload is no longer "the overlay" — ``read_project_overlay`` now composes
# the snapshot tar AND the overlay, so a project last saved in the Daytona runtime
# is retrievable in-tab instead of coming back empty. No route, DTO, or wire shape
# changed; the docstring here would otherwise describe the pre-fix behaviour.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.codeproject import lifecycle as codeproject_lifecycle
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codeproject.dto import (
    CodeProjectListResponse,
    CodeProjectResponse,
    ConsumePromptRequest,
    CreateProjectRequest,
    ProjectFilesResponse,
    ProjectFileWriteResponse,
    PutProjectFileRequest,
    RenameProjectRequest,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.websandbox import durability as websandbox_durability
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
    return CodeProjectListResponse(items=[codeproject_service.view_to_wire(v) for v in views])


@router.get("/{project_id}", response_model=CodeProjectResponse)
async def get_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> CodeProjectResponse:
    """Read one project WITHOUT opening it (owner-scoped).

    Deliberately side-effect free, unlike ``POST /{project_id}/open``: the client
    needs the project's shape (repo, name) to decide which runtime to open it in
    — the in-tab BrowserPod pod or a Daytona VM — and ``open`` cold-provisions a
    Daytona VM as a side effect. Routing on ``open`` would therefore provision a
    VM for every project even when it is about to run entirely in the browser.
    """
    workspace_id = _require_workspace(ctx)
    view = await codeproject_service.get_project(workspace_id, ctx.user_id, project_id)
    return codeproject_service.view_to_wire(view)


@router.post("/{project_id}/open", response_model=WebSandboxResponse)
async def open_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> WebSandboxResponse:
    """Open a project → a READY sandbox to connect to (reuse or provision fresh)."""
    workspace_id = _require_workspace(ctx)
    sandbox = await codeproject_lifecycle.open_project(workspace_id, ctx.user_id, project_id)
    return websandbox_service.view_to_wire(sandbox)


@router.patch("/{project_id}", response_model=CodeProjectResponse)
async def rename_project(
    project_id: str,
    body: RenameProjectRequest,
    ctx: RequestContext = Depends(request_context),
) -> CodeProjectResponse:
    """Rename a project's display name (owner-scoped)."""
    workspace_id = _require_workspace(ctx)
    view = await codeproject_service.rename_project(workspace_id, ctx.user_id, project_id, body)
    return codeproject_service.view_to_wire(view)


@router.patch("/{project_id}/consume-prompt", response_model=CodeProjectResponse)
async def consume_prompt(
    project_id: str,
    body: ConsumePromptRequest,
    ctx: RequestContext = Depends(request_context),
) -> CodeProjectResponse:
    """Mark the initial build prompt consumed on build-turn start (owner-scoped).

    ``consumed=True`` (the default) latches the prompt so a reopen doesn't re-run
    the auto-build; ``consumed=False`` re-arms it for a retry-build. Idempotent.
    """
    workspace_id = _require_workspace(ctx)
    view = await codeproject_service.mark_initial_prompt_consumed(
        workspace_id, ctx.user_id, project_id, body
    )
    return codeproject_service.view_to_wire(view)


# ---------------------------------------------------------------------------
# B1 — project file sync for the in-browser runtime.
# ---------------------------------------------------------------------------


@router.put("/{project_id}/file", response_model=ProjectFileWriteResponse)
async def put_project_file(
    project_id: str,
    body: PutProjectFileRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProjectFileWriteResponse:
    """Persist one file the in-browser runtime wrote (owner-scoped).

    Called on every editor / agent save in a tab-hosted project. Last-write-wins
    per path: a second write to the same path replaces the overlay entry rather
    than adding one.
    """
    workspace_id = _require_workspace(ctx)
    path, file_id = await websandbox_durability.put_project_file(
        workspace_id, ctx.user_id, project_id, body.path, body.content
    )
    return ProjectFileWriteResponse(ok=True, path=path, fileId=file_id)


@router.delete("/{project_id}/file", status_code=204)
async def delete_project_file(
    project_id: str,
    path: str = Query(..., min_length=1, max_length=1024),
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Drop one persisted file so a reopen doesn't resurrect it (owner-scoped)."""
    workspace_id = _require_workspace(ctx)
    await websandbox_durability.drop_project_file(workspace_id, ctx.user_id, project_id, path)
    return Response(status_code=204)


@router.get("/{project_id}/files", response_model=ProjectFilesResponse)
async def read_project_files(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProjectFilesResponse:
    """Read every persisted file back (owner-scoped) — the restore payload.

    Both durability tiers, merged: the snapshot tar a Daytona session wrote (minus
    the regenerable trees) as the baseline, with the per-file overlay on top. The
    client re-materializes the starter scaffold from the starter id, then replays
    this map over it — so a project saved in EITHER runtime reopens intact here.
    """
    workspace_id = _require_workspace(ctx)
    files = await websandbox_durability.read_project_overlay(workspace_id, ctx.user_id, project_id)
    return ProjectFilesResponse(files=files)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Delete a project and tear down its bound sandbox VM (owner-scoped)."""
    workspace_id = _require_workspace(ctx)
    await codeproject_lifecycle.delete_project(workspace_id, ctx.user_id, project_id)
    return Response(status_code=204)
