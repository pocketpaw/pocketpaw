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
# Updated 2026-06-24 (BC-10): added GET /billing/site-plans — the PER-SITE plan
#   catalog (each tier -> annual price + the Cloudflare features it resells). Like
#   /billing/plans it is tenant-independent (no workspace scoping); the frontend
#   (BC-11) reads it to render the publish tier picker.
# Updated 2026-06-28 (fix/billing-checkout-sessions): POST /billing/subscribe now
#   reads the buyer's Origin (fallback Referer) header via ``_request_origin`` and
#   threads it to ``billing_service.subscribe(origin=...)`` so the Dodo checkout
#   session returns the buyer to the app's billing page after pay / cancel (the
#   prior payment-link checkout had nowhere to send them).
# Updated 2026-06-29 (feat/billing-usage-endpoint): added GET /billing/usage — the
#   per-workspace USAGE graph (daily usage by model over a date range; spend
#   reported in credits). Workspace-scoped via ``current_workspace_id`` (same auth
#   as top-up / subscribe); logic lives in ``billing.usage``. A brand-new workspace
#   with no usage returns an empty 200.
# Updated 2026-06-29 (fix/billing-usage-ledger-source): GET /billing/usage now
#   sources the graph from the workspace's CREDIT LEDGER (the wallet's own meter,
#   mode-agnostic) rather than the LiteLLM proxy, so the chart matches the wallet in
#   every metering mode. The route surface is unchanged (same path, auth, and
#   ``start_date`` / ``end_date`` query params) — only the docstring wording below
#   is updated to reflect the source.
# Updated 2026-07-08 (feat/billing-cancel-downgrade): added POST /billing/cancel —
#   cancel the caller's workspace's ACTIVE recurring subscription (no body;
#   workspace-scoped via ``current_workspace_id``, same auth as topup / subscribe).
#   Thin adapter over ``billing_service.cancel``; the plan revert lands reactively on
#   the ``subscription.cancelled`` webhook, and a workspace with no active
#   subscription gets 402 ``billing.no_active_subscription``.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.billing import service as billing_service
from pocketpaw_ee.cloud.billing import site_plans as site_plan_catalog
from pocketpaw_ee.cloud.billing import usage as usage_service
from pocketpaw_ee.cloud.billing.dto import (
    CancelSubscriptionResponse,
    CreateSubscriptionRequest,
    CreateSubscriptionResponse,
    CreateTopupRequest,
    CreateTopupResponse,
    WorkspaceUsageResponse,
)
from pocketpaw_ee.cloud.entitlements.dto import (
    PlanCatalogResponse,
    SitePlanCatalogResponse,
    plan_tier_to_dto,
    site_plan_tier_to_dto,
)
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


@router.get("/site-plans", response_model=SitePlanCatalogResponse)
async def list_billing_site_plans() -> SitePlanCatalogResponse:
    """List the PER-SITE plan catalog — every tier with its annual price + the
    Cloudflare features it resells (BC-10 provisions those when a domain is added).

    Tenant-independent: the per-site catalog is the same for every workspace, so
    no workspace scoping (mirrors GET /billing/plans). Cheapest tier first.
    """
    return SitePlanCatalogResponse(
        site_plans=[site_plan_tier_to_dto(t) for t in site_plan_catalog.list_site_plans()]
    )


# ``YYYY-MM-DD`` — a light date-shape guard at the edge so a malformed param 422s
# before the service is reached (the service re-validates + clamps the span).
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


@router.get("/usage", response_model=WorkspaceUsageResponse)
async def get_usage(
    workspace_id: str = Depends(current_workspace_id),
    start_date: str | None = Query(
        default=None,
        pattern=_DATE_PATTERN,
        description="Start of the window, YYYY-MM-DD. Defaults to 30 days ago when omitted.",
    ),
    end_date: str | None = Query(
        default=None,
        pattern=_DATE_PATTERN,
        description="End of the window, YYYY-MM-DD. Defaults to today when omitted.",
    ),
) -> WorkspaceUsageResponse:
    """Return the caller's workspace's daily usage, broken down by model.

    Daily usage (spend in CREDITS and request count per model) over
    ``[start_date, end_date]``, sourced from the workspace's CREDIT LEDGER (the
    wallet's own meter) and scoped to that workspace — so the chart matches the
    wallet in every metering mode. When both dates are omitted the window defaults
    to the last 30 days. The response stays DAILY — the frontend aggregates to
    weekly / monthly and filters by model client-side. (``tokens`` is reported as 0:
    the ledger does not carry a per-entry token count.)

    A workspace with no spend in the window returns an empty contract (no models,
    no buckets, total 0) at HTTP 200 — not an error.
    """
    return await usage_service.get_workspace_usage(
        workspace_id,
        start_date=start_date,
        end_date=end_date,
    )


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


def _request_origin(request: Request) -> str:
    """The buyer's app origin, for building the post-checkout return_url.

    Prefer the ``Origin`` header (sent by the browser on the fetch); fall back to
    deriving ``scheme://host`` from ``Referer`` when Origin is absent (some
    same-origin navigations omit Origin). Empty string when neither is usable —
    the service then falls back to the ``dodo_checkout_return_base`` config.
    """
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return origin
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        from urllib.parse import urlsplit

        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return ""


@router.post("/subscribe", response_model=CreateSubscriptionResponse)
async def create_subscription(
    body: CreateSubscriptionRequest,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> CreateSubscriptionResponse:
    """Subscribe the caller's workspace to ``plan_key`` — recurring checkout.

    Returns the hosted-checkout url the buyer is redirected to. The plan is NOT
    upgraded and credits are NOT granted here — both land when Dodo posts a
    verified ``subscription.active`` to the public webhook; each renewal then
    grants the tier's monthly allotment additively (unused credits roll over).

    The buyer's ``Origin`` (fallback ``Referer``) header is threaded into the
    checkout's return_url / cancel_url so Dodo returns them to the app's billing
    page after pay / cancel — without it the buyer is stranded on the gateway.
    """
    result = await billing_service.subscribe(
        workspace_id=workspace_id,
        user_id=user_id,
        plan_key=body.plan_key,
        origin=_request_origin(request),
    )
    return CreateSubscriptionResponse(checkout_url=result["checkout_url"])


@router.post("/cancel", response_model=CancelSubscriptionResponse)
async def cancel_subscription(
    workspace_id: str = Depends(current_workspace_id),
) -> CancelSubscriptionResponse:
    """Cancel the caller's workspace's ACTIVE recurring subscription. No body.

    Tells the gateway to stop billing the workspace's active subscription. The plan
    revert (``Workspace.plan`` -> free) is NOT applied here — it lands reactively
    when Dodo posts the verified ``subscription.cancelled`` webhook (mirroring how
    /subscribe defers the upgrade to ``subscription.active``). Returns 402
    ``billing.no_active_subscription`` when the workspace has no active subscription.
    """
    result = await billing_service.cancel(workspace_id=workspace_id)
    return CancelSubscriptionResponse(ok=result["ok"])
