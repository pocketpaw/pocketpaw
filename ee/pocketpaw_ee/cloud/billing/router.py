# ee/pocketpaw_ee/cloud/billing/router.py — the authenticated billing surface
# (BC-2, the Gateway primitive; BC-6, the Plan catalog read).
#
# Authenticated routes, gated on a live license:
#   * POST /billing/topup — buy credits; returns a Dodo HOSTED-CHECKOUT url
#     (scoped to the caller's CURRENT workspace via ``current_workspace_id`` /
#     ``current_user_id``).
#   * GET  /billing/plans — list the PLAN CATALOG (each tier -> monthly credit
#     allotment + Dodo product id + features). Tenant-independent: the catalog is
#     the same for every workspace, so no workspace scoping (BC-6).
#
# THIN adapter per the "primitive = service + thin adapters" shape — top-up logic
# lives in ``billing.service``; the catalog is built by ``billing.plans``. The
# PUBLIC inbound webhook lives in ``billing.webhooks`` (no auth) and is mounted
# SEPARATELY in mount_cloud(). The per-workspace ENTITLEMENTS resolver (GET
# /entitlements) lives on its own ``entitlements.router`` (it IS workspace-scoped).
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new entity.
# Updated 2026-06-24 (BC-6): added GET /billing/plans (the plan catalog read).
# Updated 2026-06-24 (BC-7): added POST /billing/subscribe — open a recurring
#   checkout for a plan tier (returns a hosted url; the plan upgrade + first credit
#   grant land on the verified subscription.active webhook, not here).

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.billing import service as billing_service
from pocketpaw_ee.cloud.billing.dto import (
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    CreateTopupRequest,
    CreateTopupResponse,
)
from pocketpaw_ee.cloud.entitlements.dto import PlanCatalogResponse, plan_tier_to_dto
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(prefix="/billing", tags=["Billing"], dependencies=[Depends(require_license)])


@router.get("/plans", response_model=PlanCatalogResponse)
async def list_billing_plans() -> PlanCatalogResponse:
    """List the plan catalog — every tier with its allotment + features.

    Tenant-independent: the catalog is the same for every workspace (features
    come straight from ``PLAN_FEATURES``; allotments from the billing config
    constants). Cheapest tier first.
    """
    return PlanCatalogResponse(plans=[plan_tier_to_dto(t) for t in plan_catalog.list_plans()])


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


@router.post("/subscribe", response_model=CreateSubscriptionResponse)
async def create_subscription(
    body: CreateSubscriptionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> CreateSubscriptionResponse:
    """Subscribe the caller's workspace to ``plan_key`` — recurring checkout.

    Returns the hosted-checkout url the buyer is redirected to. The plan is NOT
    upgraded and credits are NOT granted here — both land when Dodo posts a
    verified ``subscription.active`` to the public webhook; each renewal then
    grants the tier's monthly allotment additively (unused credits roll over).
    """
    result = await billing_service.subscribe(
        workspace_id=workspace_id,
        user_id=user_id,
        plan_key=body.plan_key,
    )
    return CreateSubscriptionResponse(checkout_url=result["checkout_url"])
