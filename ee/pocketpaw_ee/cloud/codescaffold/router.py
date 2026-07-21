# router.py — Thin FastAPI adapter for the scaffold surface (CS-1).
#
# Created 2026-07-21 (feat/codescaffold). Two routes:
#
#   POST /codescaffold/plan     prompt -> what we intend to build (pure, instant)
#   POST /codescaffold/compose  recipes -> the composed source map
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
