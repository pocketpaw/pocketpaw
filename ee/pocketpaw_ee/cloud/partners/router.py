# ee/pocketpaw_ee/cloud/partners/router.py — thin HTTP layer for Paw Partners.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1).
#   /partners/me, /partners/clients[/{client_id}]  — tenant routes, guarded with
#   ``require_action_any_workspace("fabric.read" | "fabric.write")`` exactly like
#   ``cloud/leads/router.py``. The operator switch is ``cloud/platform/partners.py``.
# Updated 2026-10-02 (feat/partners-sell, PH-2): GET /partners/offers and
#   GET /partners/sites (fabric.read), POST /partners/sell — guarded by
#   ``sites.buy_plan`` (ADMIN) because a sale spends the workspace wallet.
# Updated 2026-10-02 (feat/partners-earnings, PH-11): GET /partners/summary and
#   GET /partners/earnings?months= (fabric.read, active partner).
# Updated 2026-10-02 (feat/partners-commissions, PH-13): POST /partners/pay-link
#   (``sites.buy_plan``, like /sell: a paid link changes the site's plan and rail)
#   — a one-time link the partner's client pays for a site's year.

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
from pocketpaw_ee.cloud.partners import service
from pocketpaw_ee.cloud.partners.dto import (
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerEarningsMonthOut,
    PartnerOfferOut,
    PartnerPayLinkOut,
    PartnerPayLinkRequest,
    PartnerProfileOut,
    PartnerSaleOut,
    PartnerSellRequest,
    PartnerSiteOut,
    PartnerSummaryOut,
)

router = APIRouter(prefix="/partners", tags=["partners"])

Ctx = Annotated[RequestContext, Depends(request_context)]
_READ = [Depends(require_action_any_workspace("fabric.read"))]
_WRITE = [Depends(require_action_any_workspace("fabric.write"))]
# Selling spends the workspace wallet — the same admin action a paid publish needs.
_BUY = [Depends(require_action_any_workspace("sites.buy_plan"))]


@router.get("/me", response_model=PartnerProfileOut, dependencies=_READ)
async def get_me(ctx: Ctx) -> PartnerProfileOut:
    return await service.get_profile(ctx)


@router.get("/clients", response_model=list[PartnerClientOut], dependencies=_READ)
async def list_clients(ctx: Ctx) -> list[PartnerClientOut]:
    return await service.list_clients(ctx)


@router.post("/clients", response_model=PartnerClientOut, status_code=201, dependencies=_WRITE)
async def create_client(body: PartnerClientCreateRequest, ctx: Ctx) -> PartnerClientOut:
    return await service.create_client(ctx, body=body)


@router.patch("/clients/{client_id}", response_model=PartnerClientOut, dependencies=_WRITE)
async def update_client(
    client_id: str, body: PartnerClientUpdateRequest, ctx: Ctx
) -> PartnerClientOut:
    return await service.update_client(ctx, client_id=client_id, body=body)


@router.delete("/clients/{client_id}", status_code=204, dependencies=_WRITE)
async def delete_client(client_id: str, ctx: Ctx) -> Response:
    """Delete = Fabric ARCHIVE: the client leaves every read, but the org journal
    keeps the full history, including the WhatsApp number and GSTIN. No erasure."""
    await service.delete_client(ctx, client_id=client_id)
    return Response(status_code=204)


@router.get("/offers", response_model=list[PartnerOfferOut], dependencies=_READ)
async def list_offers(ctx: Ctx) -> list[PartnerOfferOut]:
    return await service.list_offers(ctx)


@router.post("/sell", response_model=PartnerSaleOut, dependencies=_BUY)
async def sell(body: PartnerSellRequest, ctx: Ctx) -> PartnerSaleOut:
    return await service.sell(ctx, body=body)


@router.post("/pay-link", response_model=PartnerPayLinkOut, dependencies=_BUY)
async def pay_link(body: PartnerPayLinkRequest, ctx: Ctx) -> PartnerPayLinkOut:
    return await service.create_pay_link(ctx, body=body)


@router.get("/sites", response_model=list[PartnerSiteOut], dependencies=_READ)
async def list_sites(
    ctx: Ctx, due_within_days: Annotated[int | None, Query(ge=0, le=3660)] = None
) -> list[PartnerSiteOut]:
    return await service.list_sites(ctx, due_within_days=due_within_days)


@router.get("/summary", response_model=PartnerSummaryOut, dependencies=_READ)
async def get_summary(ctx: Ctx) -> PartnerSummaryOut:
    return await service.summary(ctx)


@router.get("/earnings", response_model=list[PartnerEarningsMonthOut], dependencies=_READ)
async def get_earnings(
    ctx: Ctx, months: Annotated[int, Query(ge=1, le=24)] = 12
) -> list[PartnerEarningsMonthOut]:
    return await service.earnings(ctx, months=months)
