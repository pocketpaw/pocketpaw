# ee/pocketpaw_ee/sites/files_router.py — the HTTP surface for a PROJECT site's files,
# so the builder's Code view can list, read and save the repo the agent edits.
#
# Five routes under ``/sites/by-pocket/{pocket_id}/files``: list, read one file, write
# many, patch one with exact-once ``{old, new}`` blocks, delete many. Each is a thin
# shell over ``sites.project_tools``, the same functions the agent's file tools call,
# so the path policy, size caps, lockfile rule and "every write queues the draft build"
# behaviour cannot drift between the two.
#
# Auth, the same as A1's build routes: a session (``request_context``), the "sites"
# plan feature, and ``fabric.write`` (owner / editor) on every route, reads included,
# since the source is the author's own. The pocket is resolved through
# ``sites.service.project_pocket``: the pockets read rule plus an explicit workspace
# match (another workspace's pocket is a 404) and engine ``project`` (anything else is
# 422 ``sites.not_a_project``). Writes also pass ``pockets.service`` edit access.
# Its own router module so the route-auth audit (ROUTER_MODULES) can name it.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.sites import project_tools
from pocketpaw_ee.sites.dto import (
    ProjectFileContentResponse,
    ProjectFileListResponse,
    ProjectFilePatchRequest,
    ProjectFilesDeleteRequest,
    ProjectFilesWriteRequest,
    ProjectFileWriteResponse,
)

router = APIRouter(
    tags=["Sites"],
    dependencies=[Depends(require_plan_feature("sites"))],
)

_FILES = "/sites/by-pocket/{pocket_id}/files"


@router.get(_FILES, response_model=ProjectFileListResponse)
async def list_project_files(
    pocket_id: str,
    prefix: str | None = Query(default=None, max_length=512),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ProjectFileListResponse:
    """Every file of a project site as ``{path, size}`` (UTF-8 bytes), sorted by path;
    ``prefix`` keeps the paths that start with it."""
    result = await project_tools.list_files(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id, prefix=prefix
    )
    return ProjectFileListResponse(**result)


@router.get(f"{_FILES}/content", response_model=ProjectFileContentResponse)
async def read_project_file(
    pocket_id: str,
    path: str = Query(..., min_length=1, max_length=512),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ProjectFileContentResponse:
    """One file, complete (never truncated: the editor saves it back). 404 for a path
    that is not in the project."""
    files = await project_tools.read_files(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id, paths=[path]
    )
    return ProjectFileContentResponse(**files[0])


@router.put(_FILES, response_model=ProjectFileWriteResponse)
async def write_project_files(
    pocket_id: str,
    body: ProjectFilesWriteRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ProjectFileWriteResponse:
    """Create or overwrite files (full contents) in one draft save, then queue the
    draft build. One refused path or size saves nothing."""
    result = await project_tools.write_files(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id, files=body.files
    )
    return ProjectFileWriteResponse(**result)


@router.post(f"{_FILES}/patch", response_model=ProjectFileWriteResponse)
async def patch_project_file(
    pocket_id: str,
    body: ProjectFilePatchRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ProjectFileWriteResponse:
    """Apply ``edits`` to one existing file; each ``old`` must match the running text
    exactly once (422 otherwise, nothing saved)."""
    result = await project_tools.patch_file(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        path=body.path,
        edits=[e.model_dump() for e in body.edits],
    )
    return ProjectFileWriteResponse(**result)


@router.delete(_FILES, response_model=ProjectFileWriteResponse)
async def delete_project_files(
    pocket_id: str,
    body: ProjectFilesDeleteRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ProjectFileWriteResponse:
    """Delete files. Every path must exist (404, nothing deleted otherwise);
    package.json cannot be deleted (422)."""
    result = await project_tools.delete_files(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id, paths=body.paths
    )
    return ProjectFileWriteResponse(**result)
