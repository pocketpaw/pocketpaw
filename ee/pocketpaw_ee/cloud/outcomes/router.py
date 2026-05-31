# router.py — FastAPI router for the pocket-outcomes entity.
# Created: 2026-05-22 (RFC 05 M2b.2) — exposes `GET /api/v1/outcomes`, the
#   count surface over the workspace outcome ledger. Thin: parses the
#   query, delegates to `outcomes_service.count_outcomes`. Never raises
#   HTTPException — CloudError → JSON via `_core.http`.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.outcomes import service as outcomes_service
from pocketpaw_ee.cloud.outcomes.dto import CountOutcomesRequest, OutcomeCountResponse
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

router = APIRouter(
    prefix="/outcomes",
    tags=["Outcomes"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "",
    response_model=OutcomeCountResponse,
    dependencies=[Depends(require_action_any_workspace("outcomes.read"))],
)
async def count_outcomes(
    request: Request,
    pocket_id: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO-8601 lower bound on occurred_at"),
    ctx: RequestContext = Depends(request_context),
) -> OutcomeCountResponse:
    """Count recorded pocket outcomes for the caller's workspace.

    Tenancy comes from the auth context — a ``workspace_id`` query param
    is rejected so a caller cannot read another workspace's ledger.
    """
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "outcomes.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    body = CountOutcomesRequest(pocket_id=pocket_id, since=since)
    return await outcomes_service.count_outcomes(ctx.workspace_id or "", body)
