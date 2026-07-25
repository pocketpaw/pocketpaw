# router.py — Thin FastAPI adapter for the Code Mode delegate channel.
#
# History: this router used to expose a second route, ``POST /codeagent/turn``,
# which ran a whole stateless agent turn (CA-1..CA-4). That turn-agent was removed
# 2026-07-23 (remove/codeagent-turn-agent): the /code surface runs on the MAIN
# PocketPaw cloud agent now, so a parallel in-module agent was redundant. One
# route remains:
#
#   POST /codeagent/resolve — hand a delegated task's result back to the backend.
#
# It is the return leg of a call the BACKEND started: one of the main agent's
# file tools pushes a ``code_delegate`` SSE frame and parks, the browser runs the
# file-session verb (it is the only side that can — a WebContainer project lives
# in the tab), and this route wakes the parked tool. It spends no money and calls no
# model; the workspace check here is genuine tenancy, since the correlation id it
# carries names another user's parked turn if it names anything at all.
#
# License-gated and context-authenticated like every other cloud router, and it
# never raises HTTPException — `_core.http` maps CloudError to JSON.
from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.codeagent import service as codeagent_service
from pocketpaw_ee.cloud.codeagent.dto import (
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
