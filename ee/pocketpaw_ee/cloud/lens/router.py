# ee/pocketpaw_ee/cloud/lens/router.py — /api/v1/lens/* (agent health proxy).
#
# Thin adapter over ``service.py``; mirrors the paw-lens read API 1:1:
#   GET  /lens/overview · /lens/agents · /lens/monitors · /lens/monitors/{slug}
#   GET  /lens/issues?status= · /lens/issues/{fingerprint}
#   GET  /lens/runs?automation=&status=&limit= · /lens/runs/{trace_id}
#   GET  /lens/runs/{trace_id}/spans/{span_id}
#   POST /lens/issues/{fingerprint}/mute {minutes} · /lens/issues/{fingerprint}/resolve
# GETs accept ``since``; overview, issues, agents and the runs list also take
# ``agent_id``. License-gated; the workspace is the caller's active workspace.
# Any client-sent ``workspace_id`` is not a declared param, so it is dropped.
# Path params and ``automation`` must match ``_SAFE_ID``, ``agent_id`` must
# match ``_AGENT_ID`` (422 otherwise) so nothing can path-inject into the
# upstream URL; a leading alphanumeric blocks ``.``/``..``.
#
# Reads are member-level. Mute/resolve are writes that silence alerting for the
# whole workspace, so they require ``lens.manage`` (ADMIN). Responses may be
# lists, hence ``response_model=None``.

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query

from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_action_any_workspace
from pocketpaw_ee.cloud.lens import service
from pocketpaw_ee.cloud.lens.dto import MuteIssueRequest
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/lens",
    tags=["Lens"],
    dependencies=[Depends(require_license)],
)

_MANAGE = [Depends(require_action_any_workspace("lens.manage"))]

_SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_AGENT_ID = r"^[A-Za-z0-9_-]{1,64}$"

WorkspaceId = Annotated[str, Depends(current_workspace_id)]
Since = Annotated[str | None, Query(max_length=64)]
Fingerprint = Annotated[str, Path(pattern=_SAFE_ID)]
TraceId = Annotated[str, Path(pattern=_SAFE_ID)]
Slug = Annotated[str, Path(pattern=_SAFE_ID)]
SpanId = Annotated[str, Path(pattern=_SAFE_ID)]
AgentId = Annotated[str | None, Query(pattern=_AGENT_ID)]


@router.get("/overview", response_model=None)
async def overview(workspace_id: WorkspaceId, since: Since = None, agent_id: AgentId = None) -> Any:
    return await service.overview(workspace_id, since, agent_id)


@router.get("/issues", response_model=None)
async def list_issues(
    workspace_id: WorkspaceId,
    status: Literal["open", "muted", "resolved"] | None = None,
    since: Since = None,
    agent_id: AgentId = None,
) -> Any:
    return await service.list_issues(workspace_id, status, since, agent_id)


@router.get("/issues/{fingerprint}", response_model=None)
async def get_issue(
    workspace_id: WorkspaceId, fingerprint: Fingerprint, since: Since = None
) -> Any:
    return await service.get_issue(workspace_id, fingerprint, since)


@router.post("/issues/{fingerprint}/mute", response_model=None, dependencies=_MANAGE)
async def mute_issue(
    workspace_id: WorkspaceId, fingerprint: Fingerprint, body: MuteIssueRequest
) -> Any:
    return await service.mute_issue(workspace_id, fingerprint, body.minutes)


@router.post("/issues/{fingerprint}/resolve", response_model=None, dependencies=_MANAGE)
async def resolve_issue(workspace_id: WorkspaceId, fingerprint: Fingerprint) -> Any:
    return await service.resolve_issue(workspace_id, fingerprint)


@router.get("/runs", response_model=None)
async def list_runs(
    workspace_id: WorkspaceId,
    agent_id: AgentId = None,
    automation: Annotated[str | None, Query(pattern=_SAFE_ID)] = None,
    status: Literal["ok", "error"] | None = None,
    since: Since = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> Any:
    return await service.list_runs(workspace_id, agent_id, automation, status, since, limit)


@router.get("/runs/{trace_id}", response_model=None)
async def get_run(workspace_id: WorkspaceId, trace_id: TraceId, since: Since = None) -> Any:
    return await service.get_run(workspace_id, trace_id, since)


@router.get("/runs/{trace_id}/spans/{span_id}", response_model=None)
async def get_span(workspace_id: WorkspaceId, trace_id: TraceId, span_id: SpanId) -> Any:
    return await service.get_span(workspace_id, trace_id, span_id)


@router.get("/agents", response_model=None)
async def list_agents(
    workspace_id: WorkspaceId, since: Since = None, agent_id: AgentId = None
) -> Any:
    return await service.list_agents(workspace_id, since, agent_id)


@router.get("/monitors", response_model=None)
async def list_monitors(workspace_id: WorkspaceId, since: Since = None) -> Any:
    return await service.list_monitors(workspace_id, since)


@router.get("/monitors/{slug}", response_model=None)
async def get_monitor(workspace_id: WorkspaceId, slug: Slug, since: Since = None) -> Any:
    return await service.get_monitor(workspace_id, slug, since)
