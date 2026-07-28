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
# Updated 2026-07-28 (feat/growth-mcp): PATCH /drafts/{id} — edit a draft's copy
# (subject / body / demo_url) while it is still ``draft``. Anything past that is
# 403 ``draft.not_editable``: from ``proposed`` on, the stored body is what the
# Tray shows and what the worker sends. Declared ABOVE the /drafts/{id}/status
# POST for readability only — different methods, no path-match ambiguity.
# Updated 2026-07-28 (feat/growth-api-scale): GET /prospects grew the scale
# query — ``q`` (search), ``sort`` (newest|oldest|company|tier), ``cursor`` —
# and now returns ``ProspectPageResponse`` ({items, next_cursor, total})
# instead of a bare array. BREAKING for any existing consumer of the list
# route; the frontend list view is the only one and lands with it. Plus GET
# /prospects/facets — tier/status/source counts for the filter chips, declared
# ABOVE /prospects/{prospect_id} so the literal path wins the match. Plus POST
# /drafts/propose-batch — up to 100 draft ids, each proposed through the SAME
# gated path as the single-draft route (growth.manage, one Instinct proposal
# per draft, per-draft error entries).
# Updated 2026-07-27 (feat/growth-g8): LinkedIn manual queue — GET
# /linkedin/queue (proposed/approved linkedin drafts joined with prospect
# context; ``?format=md`` returns a paste-ready text/markdown export) and
# POST /linkedin/{draft_id}/mark-sent (records a manual send via the G-3
# transition machine). Deliberately manual — no LinkedIn API.
# Updated 2026-07-28 (feat/growth-projects): GET /prospects and GET
# /prospects/facets take an optional ``project_id`` — one client's pipeline.
# Query params only; NO new route, so the guard-coverage test in
# ``tests/cloud/growth/test_gate.py`` keeps its existing surface. Omitting it
# means every project, so a workspace not using them sees no change.
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
    ProspectSort,
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
    ProposeBatchRequest,
    ProposeBatchResponse,
    ProposeSendResponse,
    ProspectFacetsResponse,
    ProspectPageResponse,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateDraftRequest,
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
    response_model=ProspectPageResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_prospects(
    tier: ProspectTier | None = Query(default=None),
    status: ProspectStatus | None = Query(default=None),
    source: ProspectSource | None = Query(default=None),
    project_id: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=200),
    sort: ProspectSort = Query(default="newest"),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> ProspectPageResponse:
    """One page of prospects: ``{items, next_cursor, total}``.

    ``q`` is a case-insensitive substring search across name / company /
    domain / research_brief — the "find that one company" box. ``sort`` is
    ``newest`` (default) / ``oldest`` / ``company`` / ``tier``; the tier order
    is the declared rank a→b→c→unqualified, not a lexicographic accident.
    ``cursor`` is the previous page's ``next_cursor``, passed back unchanged;
    ``null`` there means the last page. ``total`` counts every row matching the
    filters, so the UI can say "n of m".

    ``project_id`` scopes to one client's pipeline. Omitted means every
    project, which is the whole view for a workspace not using them; an empty
    string means the rows with no client assigned."""
    return await growth_service.list_prospects(
        ctx,
        tier=tier,
        status=status,
        source=source,
        project_id=project_id,
        q=q,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )


# Registered BEFORE /prospects/{prospect_id}: FastAPI matches in declaration
# order, so a literal path that could also read as an id has to come first or
# "facets" arrives as a prospect_id and 404s.
@router.get(
    "/prospects/facets",
    response_model=ProspectFacetsResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def prospect_facets(
    tier: ProspectTier | None = Query(default=None),
    status: ProspectStatus | None = Query(default=None),
    source: ProspectSource | None = Query(default=None),
    project_id: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=200),
    ctx: RequestContext = Depends(request_context),
) -> ProspectFacetsResponse:
    """Counts per tier / status / source for the filter chips.

    Takes the same filters as the list route. Each block excludes its OWN
    filter and respects the others — so with ``status=replied`` on, the tier
    counts describe the replied rows rather than collapsing to the selected
    tier. Every legal value is present, zeros included."""
    return await growth_service.prospect_facets(
        ctx, tier=tier, status=status, source=source, project_id=project_id, q=q
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


@router.patch(
    "/drafts/{draft_id}",
    response_model=DraftResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def update_draft(
    draft_id: str,
    body: UpdateDraftRequest,
    ctx: RequestContext = Depends(request_context),
) -> DraftResponse:
    """Edit a draft's copy — subject / body / demo_url, any subset.

    Only while the draft is still ``draft``: from ``proposed`` on, the stored
    body is what the human reviews in the Tray and what the worker sends, so an
    edit is refused with 403 ``draft.not_editable``. There is no ``status``
    field on the body — lifecycle moves go through the status route and the
    gate."""
    return await growth_service.update_draft(ctx, draft_id, body)


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


@router.post(
    "/drafts/propose-batch",
    response_model=ProposeBatchResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def propose_draft_send_batch(
    body: ProposeBatchRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProposeBatchResponse:
    """Propose a selection of drafts — up to 100 ids, an oversized payload
    422s at the boundary.

    Each id rides the SAME gated path as the single-draft route: one
    ``_growth_send`` Instinct proposal per draft, each approved or rejected by
    a human in the Tray. Nothing is sent here and there is no batch approval.
    Partial success — a draft that can't be proposed becomes an indexed
    ``{index, draft_id, code, message}`` entry in ``failed`` and the rest
    still go. Same ADMIN tier (``growth.manage``) as the single propose."""
    return await growth_service.propose_send_batch(ctx, body)


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
