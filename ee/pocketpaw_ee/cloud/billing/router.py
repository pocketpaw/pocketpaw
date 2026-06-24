# ee/pocketpaw_ee/cloud/billing/router.py — the authenticated billing surface
# (BC-2, the Gateway primitive).
#
# One authenticated route, scoped to the caller's CURRENT workspace (resolved via
# the standard ``current_workspace_id`` / ``current_user_id`` deps) and gated on
# a live license:
#   * POST /billing/topup — buy credits; returns a Dodo HOSTED-CHECKOUT url.
#
# THIN adapter per the "primitive = service + thin adapters" shape — all logic
# lives in ``billing.service``. The PUBLIC inbound webhook lives in
# ``billing.webhooks`` (no auth) and is mounted SEPARATELY in mount_cloud().
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.billing import service as billing_service
from pocketpaw_ee.cloud.billing.dto import CreateTopupRequest, CreateTopupResponse
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(prefix="/billing", tags=["Billing"], dependencies=[Depends(require_license)])


@router.post("/topup", response_model=CreateTopupResponse)
async def create_topup(
    body: CreateTopupRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> CreateTopupResponse:
    """Buy ``amount_credits`` of credits for the caller's workspace.

    Returns the hosted-checkout url the buyer is redirected to. Credits are NOT
    granted here — they land when Dodo posts a verified ``payment.succeeded`` to
    the public webhook. ``amount_credits`` is integer credits (1 credit == $0.01).
    """
    result = await billing_service.create_topup(
        workspace_id=workspace_id,
        user_id=user_id,
        amount_credits=body.amount_credits,
    )
    return CreateTopupResponse(checkout_url=result["checkout_url"])
