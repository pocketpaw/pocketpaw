# router.py — FastAPI router for the Audit entity.
# Created: 2026-05-17 — Workspace-scoped /api/v1/audit. Never raises
#   HTTPException — CloudError → JSON via _core.http.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud._core.errors import CloudError
from ee.cloud.audit import service as audit_service
from ee.cloud.audit.dto import AuditListResponse, ListAuditRequest
from ee.cloud.license import require_license
from ee.cloud.shared.deps import require_action_any_workspace

router = APIRouter(
    prefix="/audit",
    tags=["Audit"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "",
    response_model=AuditListResponse,
    dependencies=[Depends(require_action_any_workspace("audit.read"))],
)
async def list_audit(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None),
    pocket_id: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None),
    ctx: RequestContext = Depends(request_context),
) -> AuditListResponse:
    if "workspace_id" in request.query_params:
        raise CloudError(
            400,
            "audit.workspace_id_forbidden",
            "workspace_id is taken from auth context, not query",
        )
    body = ListAuditRequest(
        q=q,
        category=category,  # type: ignore[arg-type]
        pocket_id=pocket_id,
        actor=actor,
        limit=limit,
        cursor=cursor,
    )
    return await audit_service.agent_list_audit(ctx, body)
