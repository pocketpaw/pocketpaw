# ee/pocketpaw_ee/cloud/growth/router.py — FastAPI router for /growth, mounted
# under ``/api/v1``. A thin shell over ``growth.service``: parse, delegate,
# return DTOs. Every route carries the license gate, ``request_context`` and a
# per-route RBAC guard (pinned by the guard-coverage test in test_gate.py):
# reads ``growth.read`` (MEMBER), authoring ``growth.write`` (MEMBER), and the
# verbs that decide what reaches a prospect ``growth.manage`` (ADMIN) — propose
# and propose-batch (the tier the executor re-checks at approve time),
# LinkedIn mark-sent, the growth settings PATCH and deliver-approved.
#
# Surfaces: prospects (CRUD, bulk ingest / delete, scale list with q / sort /
# cursor, facets, research, writer-agent drafting, optional ``project_id``
# scoping); ICPs (CRUD + preview); drafts (create, list, copy edit while still
# ``draft``, lifecycle status moves, propose / propose-batch); the LinkedIn
# manual queue (JSON or ``?format=md`` export, mark-sent); per-channel
# delivery queues (``/queue/{channel}``, latest delivery attempt attached) and
# ``deliver-approved``; and the workspace growth settings (mock delivery).
#
# Invariants: the status route refuses the gate-owned targets ``approved`` /
# ``sent`` (403 ``draft.gate_required``) — approval happens only in the
# Instinct Tray. Literal paths (``/prospects/facets``) are declared above the
# ``{prospect_id}`` route they would otherwise be swallowed by.

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
    IcpStatus,
    ProspectSort,
    ProspectSource,
    ProspectStatus,
    ProspectTier,
)
from pocketpaw_ee.cloud.growth.dto import (
    BulkIngestRequest,
    BulkIngestResponse,
    CreateDraftRequest,
    CreateIcpRequest,
    CreateProspectRequest,
    DeleteProspectsRequest,
    DeleteProspectsResponse,
    DeliverApprovedResponse,
    DeliveryQueueItemResponse,
    DraftProspectRequest,
    DraftProspectResponse,
    DraftResponse,
    GrowthSettingsResponse,
    IcpPreviewResponse,
    IcpResponse,
    LinkedInQueueItemResponse,
    ProposeBatchRequest,
    ProposeBatchResponse,
    ProposeSendResponse,
    ProspectFacetsResponse,
    ProspectPageResponse,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateDraftRequest,
    UpdateGrowthSettingsRequest,
    UpdateIcpRequest,
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


@router.post(
    "/prospects/bulk-delete",
    response_model=DeleteProspectsResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def bulk_delete_prospects(
    body: DeleteProspectsRequest,
    ctx: RequestContext = Depends(request_context),
) -> DeleteProspectsResponse:
    """Delete up to 500 prospects and every draft on them. Ids that are
    malformed, unknown or in another workspace are skipped; the counts say
    what was actually removed. Message-log rows are kept."""
    return await growth_service.delete_prospects(ctx, body.ids)


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


@router.delete(
    "/prospects/{prospect_id}",
    status_code=204,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def delete_prospect(
    prospect_id: str,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Delete one prospect and its drafts, through the same service path as
    bulk delete. 404 when the prospect is not in the caller's workspace."""
    await growth_service.delete_prospect(ctx, prospect_id)
    return Response(status_code=204)


@router.post(
    "/prospects/{prospect_id}/research",
    response_model=ProspectResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def research_prospect(
    prospect_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ProspectResponse:
    """Research this one prospect and fold the findings in: a structured
    ``research`` profile, gaps filled (never overwritten), and a tier
    suggested to an unqualified row. 503 when no research backend is wired,
    502 when the run fails or returns nothing for this domain."""
    return await growth_service.research_prospect(ctx, prospect_id)


@router.post(
    "/prospects/{prospect_id}/draft",
    response_model=DraftProspectResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def draft_prospect(
    prospect_id: str,
    body: DraftProspectRequest,
    ctx: RequestContext = Depends(request_context),
) -> DraftProspectResponse:
    """Write first-touch drafts for this prospect on every channel it can be
    reached on (or the ``channels`` asked for). Channels it cannot be reached
    on, or that already hold a first-touch draft, come back in ``skipped``.
    422 ``prospect.no_channel`` when none is left to write for."""
    return await growth_service.draft_prospect(ctx, prospect_id, body)


# ---------------------------------------------------------------------------
# ICPs (feat/growth-discovery)
# ---------------------------------------------------------------------------


@router.post(
    "/icps",
    response_model=IcpResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def create_icp(
    body: CreateIcpRequest,
    ctx: RequestContext = Depends(request_context),
) -> IcpResponse:
    """Define who this workspace wants. ``cadence`` defaults to ``off`` — the
    ICP exists and can be previewed, but nothing runs on a schedule until
    someone switches it on."""
    return await growth_service.create_icp(ctx, body)


@router.get(
    "/icps",
    response_model=list[IcpResponse],
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_icps(
    project_id: str | None = Query(default=None, max_length=64),
    status: IcpStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[IcpResponse]:
    """The workspace's ICPs, newest first. A bare list, not the prospect
    list's page envelope — a workspace holds a handful of hand-written
    profiles, not thousands of rows."""
    return await growth_service.list_icps(ctx, project_id=project_id, status=status, limit=limit)


@router.get(
    "/icps/{icp_id}",
    response_model=IcpResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def get_icp(
    icp_id: str,
    ctx: RequestContext = Depends(request_context),
) -> IcpResponse:
    return await growth_service.get_icp(ctx, icp_id)


@router.patch(
    "/icps/{icp_id}",
    response_model=IcpResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def update_icp(
    icp_id: str,
    body: UpdateIcpRequest,
    ctx: RequestContext = Depends(request_context),
) -> IcpResponse:
    """Edit an ICP. Changing ``criteria`` does not re-run anything — the next
    tick (or a preview) picks the new text up, so tuning stays free. A change
    to what the research reads (criteria, geography, exclusions,
    ``max_per_run``) clears the ICP's ``last_preview``."""
    return await growth_service.update_icp(ctx, icp_id, body)


@router.post(
    "/icps/{icp_id}/preview",
    response_model=IcpPreviewResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def preview_icp(
    icp_id: str,
    ctx: RequestContext = Depends(request_context),
) -> IcpPreviewResponse:
    """Dry-run this ICP: research once and return what it WOULD file. Writes
    no prospects; records the result on the ICP as its last preview (a failed
    attempt included), so ``GET /icps`` and ``GET /icps/{id}`` still carry it
    after a refresh. Editing the criteria, geography, exclusions or
    ``max_per_run`` clears it.

    This is how someone comes to trust an ICP before switching its cadence on
    — criteria are prose, and prose that reads precisely to its author
    routinely describes the wrong companies. Rows already in the pipeline come
    back flagged ``already_known`` rather than hidden, because a preview full
    of them is the useful signal.

    A POST: it spends a real research pass and records its result, so it is
    not safe to retry blindly and does not belong on a GET. It sits at
    ``growth.write`` for the same reason — it is not the outbound verb (it
    cannot reach a prospect), but it is not free either."""
    return await growth_service.preview_icp(ctx, icp_id)


@router.delete(
    "/icps/{icp_id}",
    status_code=204,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def delete_icp(
    icp_id: str,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Delete an ICP. Prospects it discovered keep their ``icp_id`` and source
    URLs — provenance records what happened, and deleting the profile does not
    un-find the companies. To stop the cron without losing the definition,
    PATCH ``status`` to ``paused`` instead."""
    await growth_service.delete_icp(ctx, icp_id)
    return Response(status_code=204)


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


# ---------------------------------------------------------------------------
# Per-channel delivery queues + growth settings
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=GrowthSettingsResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def get_growth_settings(
    ctx: RequestContext = Depends(request_context),
) -> GrowthSettingsResponse:
    """The active workspace's growth settings. ``mock_delivery`` on means an
    approved email / WhatsApp draft is delivered in-process by a fake
    provider instead of the real one."""
    return await growth_service.get_growth_settings(ctx)


@router.patch(
    "/settings",
    response_model=GrowthSettingsResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def update_growth_settings(
    body: UpdateGrowthSettingsRequest,
    ctx: RequestContext = Depends(request_context),
) -> GrowthSettingsResponse:
    """Switch mock delivery on or off. ADMIN, like the outbound verbs: it
    decides whether an approval reaches a real provider."""
    return await growth_service.update_growth_settings(ctx, body)


@router.get(
    "/queue/{channel}",
    response_model=list[DeliveryQueueItemResponse],
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def delivery_queue(
    channel: DraftChannel,
    limit: int = Query(default=100, ge=1, le=500),
    ctx: RequestContext = Depends(request_context),
) -> list[DeliveryQueueItemResponse]:
    """One channel's proposed / approved / sent drafts, newest first, each with
    its prospect, recipient (``to``), opt-in and latest delivery attempt."""
    return await growth_service.delivery_queue(ctx, channel, limit=limit)


@router.post(
    "/queue/{channel}/deliver-approved",
    response_model=DeliverApprovedResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def deliver_approved(
    channel: DraftChannel,
    ctx: RequestContext = Depends(request_context),
) -> DeliverApprovedResponse:
    """Start mock delivery for every approved draft on this channel with no
    attempt in flight. 422 ``queue.not_deliverable`` for linkedin, 409
    ``growth.mock_delivery_off`` when mock delivery is off."""
    return await growth_service.deliver_approved(ctx, channel)
