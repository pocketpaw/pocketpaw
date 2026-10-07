# ee/pocketpaw_ee/cloud/partners/router.py — thin HTTP layer for Paw Partners.
#
# Tenant routes (guarded with ``require_action_any_workspace`` exactly like
# ``cloud/leads/router.py``): /partners/me, /partners/me/profile (PATCH),
# /partners/clients[/{client_id}], /offers, /sites, /summary, /earnings,
# /rewards (fabric.read / fabric.write); POST /sell and /pay-link need
# ``sites.buy_plan`` because they spend the workspace wallet. The operator
# switch is ``cloud/platform/partners.py``.
#
# Public routes, no sign-in, per-IP rate limited (``_core.rate_limit``):
# GET /partners/directory, POST /partners/apply, GET /partners/{slug}. The slug
# catch-all is registered LAST so every fixed segment wins; the reserved-slug
# list in ``partners.domain`` keeps a partner from claiming one. Public responses
# go only through ``PartnerPublicOut``. The dashboard auth middleware lets
# /api/v1/* through and the EE auth bridge stamps a user only when a token is
# present, so no exemption entry is needed (same as /discover).

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
from pocketpaw_ee.cloud._core.rate_limit import (
    client_ip,
    rate_limit_partner_apply,
    rate_limit_partner_public,
)
from pocketpaw_ee.cloud.partners import service, service_admin
from pocketpaw_ee.cloud.partners.domain import PartnerService
from pocketpaw_ee.cloud.partners.dto import (
    PartnerApplyIn,
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerDirectoryPage,
    PartnerEarningsMonthOut,
    PartnerMeOut,
    PartnerOfferOut,
    PartnerPayLinkOut,
    PartnerPayLinkRequest,
    PartnerPublicOut,
    PartnerPublicProfileIn,
    PartnerRewardOut,
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
_PUBLIC = [Depends(rate_limit_partner_public)]


@router.get("/me", response_model=PartnerMeOut, dependencies=_READ)
async def get_me(ctx: Ctx) -> PartnerMeOut:
    return await service.get_profile(ctx)


@router.patch("/me/profile", response_model=PartnerMeOut, dependencies=_WRITE)
async def update_profile(body: PartnerPublicProfileIn, ctx: Ctx) -> PartnerMeOut:
    """Edit the caller's public partner profile; only the fields sent change."""
    return await service.update_public_profile(ctx, body)


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


@router.get("/rewards", response_model=list[PartnerRewardOut], dependencies=_READ)
async def get_rewards(ctx: Ctx) -> list[PartnerRewardOut]:
    return await service.rewards(ctx)


# ---------------------------------------------------------------- public (no sign-in)


@router.get("/directory", response_model=PartnerDirectoryPage, dependencies=_PUBLIC)
async def directory(
    city: Annotated[str | None, Query(max_length=80)] = None,
    service_: Annotated[PartnerService | None, Query(alias="service")] = None,
    cursor: Annotated[str | None, Query(max_length=160)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 24,
) -> PartnerDirectoryPage:
    """PUBLIC. Active partners who opted in, newest first; ``cursor`` is the opaque
    ``next_cursor`` of the previous page; 429 ``partners.rate_limited`` past 60
    reads a minute per IP."""
    return await service_admin.list_directory(
        city=city, service=service_, cursor=cursor, limit=limit
    )


@router.post("/apply", status_code=204, dependencies=[Depends(rate_limit_partner_apply)])
async def apply(body: PartnerApplyIn, request: Request) -> Response:
    """PUBLIC. Apply to become a partner: one proposal for the platform. 400
    ``partners.turnstile_failed``; 429 ``partners.apply_rate_limited`` past 5 an hour."""
    await service_admin.apply(body, remote_ip=client_ip(request, trusted_header_ok=True))
    return Response(status_code=204)


# Registered LAST: every fixed /partners/<segment> above wins over the slug.
@router.get("/{slug}", response_model=PartnerPublicOut, dependencies=_PUBLIC)
async def get_public(slug: str) -> PartnerPublicOut:
    """PUBLIC. One active, opted-in partner by slug; 404 otherwise."""
    return await service_admin.get_public(slug)
