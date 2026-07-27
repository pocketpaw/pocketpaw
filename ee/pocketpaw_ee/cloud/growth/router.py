# ee/pocketpaw_ee/cloud/growth/router.py — FastAPI router for the prospect
# store. Thin shell over ``ee.cloud.growth.service``: parses requests,
# delegates, returns DTOs. License gate + canonical ``request_context``
# dependency on every route — services never see raw ``Request`` objects, and
# every read/write is workspace-scoped inside the service (cross-tenant ids
# 404). Mounted under ``/api/v1`` → final paths ``/api/v1/growth/prospects``.
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — create / get /
# list (tier/status/source filters) / update. Later slices add ingestion,
# drafts, and Instinct-gated sends.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.domain import ProspectSource, ProspectStatus, ProspectTier
from pocketpaw_ee.cloud.growth.dto import (
    CreateProspectRequest,
    ProspectResponse,
    UpdateProspectRequest,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/growth/prospects", tags=["Growth"], dependencies=[Depends(require_license)]
)


@router.post("", response_model=ProspectResponse)
async def create_prospect(
    body: CreateProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.create(ctx, body)


@router.get("", response_model=list[ProspectResponse])
async def list_prospects(
    tier: ProspectTier | None = Query(default=None),
    status: ProspectStatus | None = Query(default=None),
    source: ProspectSource | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[ProspectResponse]:
    return await growth_service.list_prospects(
        ctx, tier=tier, status=status, source=source, limit=limit
    )


@router.get("/{prospect_id}", response_model=ProspectResponse)
async def get_prospect(
    prospect_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.get(ctx, prospect_id)


@router.patch("/{prospect_id}", response_model=ProspectResponse)
async def update_prospect(
    prospect_id: str,
    body: UpdateProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.update(ctx, prospect_id, body)
