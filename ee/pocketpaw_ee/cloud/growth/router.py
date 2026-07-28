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
# Updated 2026-07-27 (feat/growth-g4): POST /drafts/{id}/propose — files a
# gated ``_growth_send`` Instinct proposal and flips the draft to ``proposed``.
# The status route now refuses the gate-owned targets (``approved`` / ``sent``)
# with 403 ``draft.gate_required`` — approval happens ONLY in the Instinct
# Tray, and only the approved dispatch path may send.
# Updated 2026-07-27 (feat/growth-g4, security review F3): per-route RBAC.
# ``require_license`` alone left every route open to any authenticated member
# of any workspace. Reads take ``growth.read`` (MEMBER), authoring writes
# ``growth.write`` (MEMBER), and the OUTBOUND verb — POST
# /drafts/{id}/propose — takes ``growth.manage`` (ADMIN): the propose route
# has to sit at the same tier ``growth.executor`` re-checks at dispatch, or a
# member-filed proposal would always fail closed at approve time.
# Updated 2026-07-27 (feat/growth-g8): LinkedIn manual queue — GET
# /linkedin/queue (proposed/approved linkedin drafts joined with prospect
# context; ``?format=md`` returns a paste-ready text/markdown export) and
# POST /linkedin/{draft_id}/mark-sent (records a manual send via the G-3
# transition machine). Deliberately manual — no LinkedIn API.
# Updated 2026-07-27 (integration/growth-v1): the G-8 LinkedIn routes carry the
# G-4 per-route RBAC guards their branch predated — ``growth.read`` on the
# queue, ``growth.manage`` on mark-sent (it is an OUTBOUND verb, same tier as
# propose).

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
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
    LinkedInQueueItemResponse,
    ProposeSendResponse,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateProspectRequest,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(prefix="/growth", tags=["Growth"], dependencies=[Depends(require_license)])


@router.post(
    "/prospects",
    response_model=ProspectResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def create_prospect(
    body: CreateProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.create(ctx, body)


@router.post(
    "/prospects/bulk",
    response_model=BulkIngestResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def bulk_ingest_prospects(
    body: BulkIngestRequest,
    ctx: RequestContext = Depends(request_context),
) -> BulkIngestResponse:
    """Batch create-or-update (max 500 rows). Bad rows come back as indexed
    error entries; the rest land. Idempotent — re-posting the same payload
    updates the existing rows instead of duplicating them."""
    return await growth_service.bulk_ingest(ctx, body)


@router.get(
    "/prospects",
    response_model=list[ProspectResponse],
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_prospects(
    tier: ProspectTier | None = Query(default=None),
    status: ProspectStatus | None = Query(default=None),
    source: ProspectSource | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[ProspectResponse]:
    """``q`` is a case-insensitive substring search across name / company /
    domain / research_brief — the "find that one company" box."""
    return await growth_service.list_prospects(
        ctx, tier=tier, status=status, source=source, q=q, limit=limit
    )


@router.get(
    "/prospects/{prospect_id}",
    response_model=ProspectResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def get_prospect(
    prospect_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.get(ctx, prospect_id)


@router.patch(
    "/prospects/{prospect_id}",
    response_model=ProspectResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def update_prospect(
    prospect_id: str,
    body: UpdateProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    return await growth_service.update(ctx, prospect_id, body)


# ---------------------------------------------------------------------------
# Drafts (G-3)
# ---------------------------------------------------------------------------


@router.post(
    "/prospects/{prospect_id}/drafts",
    response_model=DraftResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def create_draft(
    prospect_id: str,
    body: CreateDraftRequest,
    ctx: RequestContext = Depends(request_context),
) -> DraftResponse:
    """Attach one channel's outreach copy to a prospect. The prospect must
    exist in the caller's workspace (404 otherwise); a new/qualified prospect
    flips to ``drafted`` on its first draft."""
    return await growth_service.create_draft(ctx, prospect_id, body)


@router.get(
    "/drafts",
    response_model=list[DraftResponse],
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
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


@router.post(
    "/drafts/{draft_id}/status",
    response_model=DraftResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def transition_draft(
    draft_id: str,
    body: TransitionDraftRequest,
    ctx: RequestContext = Depends(request_context),
) -> DraftResponse:
    """Move a draft along the lifecycle. Legal: draft→proposed→approved→sent,
    sent→replied, any non-terminal→rejected. Anything else is a 422
    ``draft.illegal_transition``. The gate-owned targets ``approved`` and
    ``sent`` are refused here with 403 ``draft.gate_required`` — they are set
    only by the Instinct send gate (G-4)."""
    return await growth_service.transition(ctx, draft_id, body)


@router.post(
    "/drafts/{draft_id}/propose",
    response_model=ProposeSendResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def propose_draft_send(
    draft_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProposeSendResponse:
    """File a gated ``_growth_send`` Instinct proposal for this draft (G-4).

    Flips the draft to ``proposed`` and returns the ``proposal_id`` a human
    approves or rejects in the Tray. NOTHING is sent by this route — approval
    enqueues the ``growth.dispatch`` job; rejection flips the draft to
    ``rejected``. A draft that cannot legally move to ``proposed`` is a 422
    ``draft.illegal_transition`` (so re-proposing is refused)."""
    return await growth_service.propose_send(ctx, draft_id)


# ---------------------------------------------------------------------------
# LinkedIn manual queue (G-8)
# ---------------------------------------------------------------------------


@router.get(
    "/linkedin/queue",
    response_model=None,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def linkedin_queue(
    format: Literal["json", "md"] = Query(default="json"),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[LinkedInQueueItemResponse] | Response:
    """The manual send queue: the workspace's linkedin drafts in
    proposed/approved, newest first, joined with prospect context.
    ``?format=md`` returns a paste-ready ``text/markdown`` export instead —
    one section per prospect with the connect note and after-accept message.
    Manual send is the feature: there is no LinkedIn API integration."""
    if format == "md":
        markdown = await growth_service.linkedin_queue_markdown(ctx, limit=limit)
        return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8")
    return await growth_service.linkedin_queue(ctx, limit=limit)


@router.post(
    "/linkedin/{draft_id}/mark-sent",
    response_model=DraftResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def mark_linkedin_sent(
    draft_id: str,
    ctx: RequestContext = Depends(request_context),
) -> DraftResponse:
    """Record a manual LinkedIn send. The draft must be linkedin-channel
    (422 ``draft.wrong_channel``) and ``approved`` — the move rides the G-3
    machine, so anything but approved→sent is a 422
    ``draft.illegal_transition``."""
    return await growth_service.mark_linkedin_sent(ctx, draft_id)
