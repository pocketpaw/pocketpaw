# router.py — FastAPI router for the pocket-outcomes entity.
# Created: 2026-05-22 (RFC 05 M2b.2) — exposes `GET /api/v1/outcomes`, the
#   count surface over the workspace outcome ledger. Thin: parses the
#   query, delegates to `outcomes_service.count_outcomes`. Never raises
#   HTTPException — CloudError → JSON via `_core.http`.
# Updated: 2026-06-11 (gap-3 outcome VALUE metering) — added
#   `GET /api/v1/outcomes/meter`, the aggregation read surface. Same
#   tenancy posture as the count endpoint (workspace_id from auth context,
#   a query param is rejected); delegates to `outcomes_service.
#   meter_outcomes`, which sums billable value BY unit over a since/until
#   window. Behind the same `outcomes.read` RBAC action — reading the
#   metered figure is the same right as reading the count.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.outcomes import service as outcomes_service
from pocketpaw_ee.cloud.outcomes.dto import (
    CountOutcomesRequest,
    MeterOutcomesRequest,
    MeterOutcomesResponse,
    OutcomeCountResponse,
)
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


@router.get(
    "/meter",
    response_model=MeterOutcomesResponse,
    dependencies=[Depends(require_action_any_workspace("outcomes.read"))],
)
async def meter_outcomes(
    request: Request,
    pocket_id: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO-8601 inclusive lower bound"),
    until: str | None = Query(default=None, description="ISO-8601 exclusive upper bound"),
    ctx: RequestContext = Depends(request_context),
) -> MeterOutcomesResponse:
    """Aggregate the caller's governed outcomes into a billable figure.

    Sums ``outcome_value`` BY ``outcome_unit`` over the optional
    ``pocket_id`` / ``since`` / ``until`` window — the "pay for governed
    outcomes" read primitive. Tenancy comes from the auth context; a
    ``workspace_id`` query param is rejected so a caller cannot meter
    another workspace's ledger. This is a READ surface — no invoice, no
    charge fires (deferred).
    """
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "outcomes.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    body = MeterOutcomesRequest(pocket_id=pocket_id, since=since, until=until)
    return await outcomes_service.meter_outcomes(ctx.workspace_id or "", body)
