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
# Updated 2026-07-27 (feat/growth-g2): POST /bulk — batch ingestion (Clay /
# directory imports) via the service's ``bulk_ingest``; 500-row cap on the DTO,
# per-row errors in the response.
# Updated 2026-07-27 (feat/growth-g3): drafts — POST /prospects/{id}/drafts,
# GET /drafts (prospect/channel/status filters), POST /drafts/{id}/status
# (illegal moves 422 ``draft.illegal_transition``). Router prefix widened from
# ``/growth/prospects`` to ``/growth`` (routes now carry ``/prospects``
# themselves) so drafts mount beside prospects — final URLs unchanged.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.domain import (
    DraftChannel,
    DraftStatus,
    ProspectSource,
    ProspectStatus,
    ProspectTier,
)
from pocketpaw_ee.cloud.growth.dto import (
    BulkIngestRequest,
    BulkIngestResponse,
    CreateDraftRequest,
    CreateProspectRequest,
    DraftResponse,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateProspectRequest,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(prefix="/growth", tags=["Growth"], dependencies=[Depends(require_license)])


@router.post("/prospects", response_model=ProspectResponse)
async def create_prospect(
    body: CreateProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.create(ctx, body)


@router.post("/prospects/bulk", response_model=BulkIngestResponse)
async def bulk_ingest_prospects(
    body: BulkIngestRequest,
    ctx: RequestContext = Depends(request_context),
) -> BulkIngestResponse:
    """Batch create-or-update (max 500 rows). Bad rows come back as indexed
    error entries; the rest land. Idempotent — re-posting the same payload
    updates the existing rows instead of duplicating them."""
    return await growth_service.bulk_ingest(ctx, body)


@router.get("/prospects", response_model=list[ProspectResponse])
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


@router.get("/prospects/{prospect_id}", response_model=ProspectResponse)
async def get_prospect(
    prospect_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.get(ctx, prospect_id)


@router.patch("/prospects/{prospect_id}", response_model=ProspectResponse)
async def update_prospect(
    prospect_id: str,
    body: UpdateProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.update(ctx, prospect_id, body)


# ---------------------------------------------------------------------------
# Drafts (G-3)
# ---------------------------------------------------------------------------


@router.post("/prospects/{prospect_id}/drafts", response_model=DraftResponse)
async def create_draft(
    prospect_id: str,
    body: CreateDraftRequest,
    ctx: RequestContext = Depends(request_context),
) -> DraftResponse:
    """Attach one channel's outreach copy to a prospect. The prospect must
    exist in the caller's workspace (404 otherwise); a new/qualified prospect
    flips to ``drafted`` on its first draft."""
    return await growth_service.create_draft(ctx, prospect_id, body)


@router.get("/drafts", response_model=list[DraftResponse])
async def list_drafts(
    prospect_id: str | None = Query(default=None),
    channel: DraftChannel | None = Query(default=None),
    status: DraftStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[DraftResponse]:
    return await growth_service.list_drafts(
        ctx, prospect_id=prospect_id, channel=channel, status=status, limit=limit
    )


@router.post("/drafts/{draft_id}/status", response_model=DraftResponse)
async def transition_draft(
    draft_id: str,
    body: TransitionDraftRequest,
    ctx: RequestContext = Depends(request_context),
) -> DraftResponse:
    """Move a draft along the lifecycle. Legal: draft→proposed→approved→sent,
    sent→replied, any non-terminal→rejected. Anything else is a 422
    ``draft.illegal_transition``."""
    return await growth_service.transition(ctx, draft_id, body)
