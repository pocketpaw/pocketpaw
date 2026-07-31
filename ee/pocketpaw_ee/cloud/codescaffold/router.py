# router.py — Thin FastAPI adapter for the scaffold surface (CS-1).
#
# Created 2026-07-21 (feat/codescaffold). Three routes:
#
#   GET  /codescaffold/starters  the catalog (CS-3; no prompt, no network)
#   POST /codescaffold/plan      prompt -> what we intend to build (pure, instant)
#   POST /codescaffold/compose   a starter id -> the source map
#
# License-gated and context-authenticated like every other cloud router, and it
# never raises HTTPException — `_core.http` maps CloudError to JSON.
#
# Tenancy note, same as `codeagent`: there is no resource to own here. Neither
# route reads or writes a row. The workspace check is about METERING and abuse
# (compose spawns a subprocess), not about authorizing access to an object,
# which is why it fails closed on a missing workspace but does no per-object
# lookup.
from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.codescaffold import service as codescaffold_service
from pocketpaw_ee.cloud.codescaffold.dto import (
    ScaffoldComposeRequest,
    ScaffoldComposeResponse,
    ScaffoldPlanRequest,
    ScaffoldPlanResponse,
    ScaffoldStartersResponse,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/codescaffold",
    tags=["CodeScaffold"],
    dependencies=[Depends(require_license)],
)


def _require_workspace(ctx: RequestContext) -> str:
    """A workspace-scoped route needs an active workspace; fail closed if absent."""
    if not ctx.workspace_id:
        raise Forbidden("codescaffold.no_workspace", "No active workspace")
    return ctx.workspace_id


@router.get("/starters", response_model=ScaffoldStartersResponse)
async def starters(
    ctx: RequestContext = Depends(request_context),
) -> ScaffoldStartersResponse:
    """The starter catalog. Reads a constant; fetches nothing.

    A GET because it is the one route here that is genuinely a resource read —
    same answer every time, safe to cache, safe to retry. `/plan` and `/compose`
    stay POSTs (a prompt is a body, and compose does real work).
    """
    _require_workspace(ctx)
    return codescaffold_service.list_starters()


@router.post("/plan", response_model=ScaffoldPlanResponse)
async def plan(
    body: ScaffoldPlanRequest,
    ctx: RequestContext = Depends(request_context),
) -> ScaffoldPlanResponse:
    """Decide what to build from a prompt. Writes nothing and runs no engine."""
    workspace_id = _require_workspace(ctx)
    return await codescaffold_service.plan(workspace_id, ctx.user_id, body)


@router.post("/compose", response_model=ScaffoldComposeResponse)
async def compose(
    body: ScaffoldComposeRequest,
    ctx: RequestContext = Depends(request_context),
) -> ScaffoldComposeResponse:
    """Compose a project and return it as a source map.

    Returns the files rather than writing them anywhere. Materializing is the
    RUNTIME's job — tar-upload for a Daytona VM, `fs.mount` for a WebContainer —
    and keeping that out of here is what lets one endpoint serve both.
    """
    workspace_id = _require_workspace(ctx)
    return await codescaffold_service.compose(workspace_id, ctx.user_id, body)
