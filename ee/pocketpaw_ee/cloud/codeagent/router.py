# router.py — Thin FastAPI adapter for the Code Mode agent turn (CA-1).
#
# Created 2026-07-21 (feat/codeagent-turn). One route:
#
#   POST /codeagent/turn — run one step of a turn against the caller's code.
#
# Modified: 2026-07-21 (CA-4). The route is no longer Ask-only — the request's
# ``mode`` selects the permission set, and Edit mode replaces the deleted
# ``POST /websandbox/{row_id}/edit``. That old route took a sandbox row id, which
# is exactly why Cmd-K could never work in a WebContainer: a project running in
# the user's tab has no row. This one takes the code in the request instead.
#
# License-gated and context-authenticated like every other cloud router, and it
# never raises HTTPException — `_core.http` maps CloudError to JSON.
#
# Tenancy note: unlike `/websandbox` and `/codeproject`, there is no resource to
# own here. The request carries its own context and the server opens nothing, so
# the workspace check is about METERING and abuse (this route spends money),
# not about authorizing access to a row. That is why it fails closed on a
# missing workspace but does no per-object lookup.
#
# Modified: 2026-07-22 (CD-1, delegate channel). Second route:
#
#   POST /codeagent/resolve — hand a delegated task's result back to the backend.
#
# It is the return leg of a call the BACKEND started: the main agent's
# ``code_mode`` tool pushes a ``code_delegate`` SSE frame and parks, the browser
# does the work (it is the only side that can — a WebContainer project lives in
# the tab), and this route wakes the parked turn. Unlike ``/turn`` it spends no
# money and calls no model; the workspace check here is genuine tenancy, since
# the correlation id it carries names another user's parked turn if it names
# anything at all.
from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.codeagent import service as codeagent_service
from pocketpaw_ee.cloud.codeagent.dto import (
    AgentTurnRequest,
    AgentTurnResponse,
    DelegateResolveRequest,
    DelegateResolveResponse,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/codeagent",
    tags=["CodeAgent"],
    dependencies=[Depends(require_license)],
)


def _require_workspace(ctx: RequestContext) -> str:
    """A workspace-scoped route needs an active workspace; fail closed if absent."""
    if not ctx.workspace_id:
        raise Forbidden("codeagent.no_workspace", "No active workspace")
    return ctx.workspace_id


@router.post("/turn", response_model=AgentTurnResponse)
async def run_turn(
    body: AgentTurnRequest,
    ctx: RequestContext = Depends(request_context),
) -> AgentTurnResponse:
    """Run one step of a turn. This endpoint never writes a file itself — in Edit
    mode it PROPOSES one, and the client holds it for the user's review."""
    workspace_id = _require_workspace(ctx)
    return await codeagent_service.run_turn(workspace_id, ctx.user_id, body)


@router.post("/resolve", response_model=DelegateResolveResponse)
async def resolve_delegate(
    body: DelegateResolveRequest,
    ctx: RequestContext = Depends(request_context),
) -> DelegateResolveResponse:
    """Deliver a delegated task's result to the backend turn waiting on it.

    404 (``code_delegate.not_found``) when nothing is parked under the given
    ``corrId`` — including a second POST for an id already answered, and one
    that arrives after the park timed out.
    """
    workspace_id = _require_workspace(ctx)
    return await codeagent_service.resolve_delegate(workspace_id, ctx.user_id, body)
