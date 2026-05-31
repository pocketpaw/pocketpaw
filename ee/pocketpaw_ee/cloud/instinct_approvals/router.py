# ee/pocketpaw_ee/cloud/instinct_approvals/router.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — REST surface
# for the RFC 03 v2 template-level approval queue. Routes are thin:
# they parse the request, delegate to the service, and return the wire
# dict the service produced. Errors propagate via ``CloudError``; the
# central ``cloud_error_handler`` maps to JSON. Never raises
# ``HTTPException`` (rule 10).

"""FastAPI router for ``instinct_approvals``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
from pocketpaw_ee.cloud.instinct_approvals.dto import (
    ApprovalDecisionRequest,
    CreateApprovalRequest,
    ListApprovalsRequest,
)

router = APIRouter(prefix="/instinct-approvals", tags=["InstinctApprovals"])


def _require_workspace(ctx: RequestContext) -> str:
    if not ctx.workspace_id:
        raise ValidationError(
            "instinct_approval.workspace_required",
            "no active workspace on this request",
        )
    return ctx.workspace_id


@router.post("")
async def create(
    body: CreateApprovalRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    """Persist a new pending approval row.

    Internal route — the dispatch wrapper
    (``pockets.instinct_dispatch.gate_action``) calls
    ``approvals_service.create_approval`` directly when the composer
    returns ``ESCALATE_APPROVAL``. The endpoint exists so tooling and
    tests can exercise the same path.
    """
    workspace_id = _require_workspace(ctx)
    return await approvals_service.create_approval(workspace_id, ctx.user_id, body)


@router.get("")
async def list_approvals(
    status: str | None = None,
    pocket_id: str | None = None,
    limit: int = 50,
    ctx: RequestContext = Depends(request_context),
) -> list[dict]:
    workspace_id = _require_workspace(ctx)
    body = ListApprovalsRequest(status=status, pocket_id=pocket_id, limit=limit)
    return await approvals_service.list_approvals(workspace_id, ctx.user_id, body)


@router.get("/{approval_id}")
async def get(
    approval_id: str,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    workspace_id = _require_workspace(ctx)
    return await approvals_service.get_approval(workspace_id, ctx.user_id, approval_id)


@router.post("/{approval_id}/approve")
async def approve(
    approval_id: str,
    body: ApprovalDecisionRequest | None = None,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    workspace_id = _require_workspace(ctx)
    return await approvals_service.approve(workspace_id, ctx.user_id, approval_id, body)


@router.post("/{approval_id}/reject")
async def reject(
    approval_id: str,
    body: ApprovalDecisionRequest | None = None,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    workspace_id = _require_workspace(ctx)
    return await approvals_service.reject(workspace_id, ctx.user_id, approval_id, body)


__all__ = ["router"]
