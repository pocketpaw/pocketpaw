# ee/pocketpaw_ee/cloud/lens/router.py — /api/v1/lens/* (agent health proxy).
#
# Thin adapter over ``service.py``; mirrors the paw-lens read API 1:1:
#   GET  /lens/overview · /lens/agents · /lens/monitors · /lens/monitors/{slug}
#   GET  /lens/issues?status= · /lens/issues/{fingerprint} · /lens/runs/{trace_id}
#   POST /lens/issues/{fingerprint}/mute {minutes} · /lens/issues/{fingerprint}/resolve
# GETs accept ``since``. License-gated; the workspace is the caller's active
# workspace. Any client-sent ``workspace_id`` is not a declared param, so it is
# dropped. Path params must match ``_SAFE_ID`` (422 otherwise) so nothing can
# path-inject into the upstream URL; a leading alphanumeric blocks ``.``/``..``.
#
# Mute/resolve are member-level: there is no ``lens.*`` action in the guards
# ACTIONS table yet. Gate them with ``require_action_any_workspace`` once one
# exists. Responses may be lists, hence ``response_model=None``.

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query

from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.lens import service
from pocketpaw_ee.cloud.lens.dto import MuteIssueRequest
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/lens",
    tags=["Lens"],
    dependencies=[Depends(require_license)],
)

_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"

WorkspaceId = Annotated[str, Depends(current_workspace_id)]
Since = Annotated[str | None, Query(max_length=64)]
Fingerprint = Annotated[str, Path(pattern=_SAFE_ID)]
TraceId = Annotated[str, Path(pattern=_SAFE_ID)]
Slug = Annotated[str, Path(pattern=_SAFE_ID)]


@router.get("/overview", response_model=None)
async def overview(workspace_id: WorkspaceId, since: Since = None) -> Any:
    return await service.overview(workspace_id, since)


@router.get("/issues", response_model=None)
async def list_issues(
    workspace_id: WorkspaceId,
    status: Literal["open", "muted", "resolved"] | None = None,
    since: Since = None,
) -> Any:
    return await service.list_issues(workspace_id, status, since)


@router.get("/issues/{fingerprint}", response_model=None)
async def get_issue(
    workspace_id: WorkspaceId, fingerprint: Fingerprint, since: Since = None
) -> Any:
    return await service.get_issue(workspace_id, fingerprint, since)


@router.post("/issues/{fingerprint}/mute", response_model=None)
async def mute_issue(
    workspace_id: WorkspaceId, fingerprint: Fingerprint, body: MuteIssueRequest
) -> Any:
    return await service.mute_issue(workspace_id, fingerprint, body.minutes)


@router.post("/issues/{fingerprint}/resolve", response_model=None)
async def resolve_issue(workspace_id: WorkspaceId, fingerprint: Fingerprint) -> Any:
    return await service.resolve_issue(workspace_id, fingerprint)


@router.get("/runs/{trace_id}", response_model=None)
async def get_run(workspace_id: WorkspaceId, trace_id: TraceId, since: Since = None) -> Any:
    return await service.get_run(workspace_id, trace_id, since)


@router.get("/agents", response_model=None)
async def list_agents(workspace_id: WorkspaceId, since: Since = None) -> Any:
    return await service.list_agents(workspace_id, since)


@router.get("/monitors", response_model=None)
async def list_monitors(workspace_id: WorkspaceId, since: Since = None) -> Any:
    return await service.list_monitors(workspace_id, since)


@router.get("/monitors/{slug}", response_model=None)
async def get_monitor(workspace_id: WorkspaceId, slug: Slug, since: Since = None) -> Any:
    return await service.get_monitor(workspace_id, slug, since)
