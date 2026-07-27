# ee/pocketpaw_ee/cloud/growth/service.py — sole owner of Prospect writes
# (service-is-repo; only this module imports ``models.prospect``). Tenancy:
# every read filters on ``workspace``; a cross-tenant id raises NotFound so
# existence never leaks. ``upsert_by_domain`` is the create-or-update seam the
# later ingestion slices (Clay / directory imports) call — keyed on
# (workspace_id, normalised domain), matching the unique index on the doc.
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — the prospect
# store. No events yet: growth has no realtime subscriber in v1, so writes
# carry ``# no-event:`` markers per the ee/cloud emit rule.
# Updated 2026-07-27 (feat/growth-g2): ``bulk_ingest`` — per-row validation +
# ``upsert_by_domain`` over a capped batch; a bad row records an indexed error
# entry and the rest proceed (idempotent upserts, so no rollback is needed).
# Updated 2026-07-27 (feat/growth-g3): drafts — ``create_draft`` (prospect must
# exist in the workspace; first draft flips a new/qualified prospect to
# ``drafted``), ``list_drafts`` (prospect/channel/status filters), and
# ``transition`` — a dumb enforcer of ``DRAFT_TRANSITIONS`` (illegal moves →
# 422 ``draft.illegal_transition``), no side effects; G-4 wires proposals on
# top. This module also owns the Draft doc writes (same "Growth" contract).
# Updated 2026-07-27 (feat/growth-g4): the Instinct send gate. The PUBLIC
# ``transition`` now refuses gate-owned targets (``approved`` / ``sent``) with
# 403 ``draft.gate_required`` — those edges belong to the gate machinery:
# ``propose_send`` files a ``_growth_send`` Instinct proposal (and flips
# draft→proposed via ``transition``), and ``gate_transition`` is the internal
# seam the growth executor / dispatch worker use to walk gate-owned edges
# (same legality table, explicit workspace_id, no RequestContext).
# Updated 2026-07-27 (feat/growth-g8): LinkedIn manual queue —
# ``linkedin_queue`` (proposed/approved linkedin drafts joined with their
# prospect via two queries, newest first), ``linkedin_queue_markdown``
# (copy-paste export grouped per prospect: connect note + after-accept
# message), and ``mark_linkedin_sent`` (channel guard + the EXISTING
# ``transition`` function, so approved→sent stays the only legal move and
# proposed→sent 422s as ``draft.illegal_transition``). Deliberately manual —
# no LinkedIn API, no automation (account-ban avoidance is the feature).
# Updated 2026-07-27 (feat/growth-g5): the dispatch-worker seams —
# ``get_draft_for_dispatch`` / ``get_prospect_for_dispatch`` (the worker is
# handed only a draft id by the queue and derives tenancy FROM the row; see the
# ``global-read`` justifications on those reads) and ``record_message_log``,
# the sole writer of the ``MessageLog`` audit doc — one row per delivery
# ATTEMPT, so a failure and its later retry both survive.
# Updated 2026-07-27 (feat/growth-g7): the follow-up sweep's data seams — the
# only Beanie access the cron sweep (``growth/followups.py``) gets, since the
# import-linter "Growth" contract keeps the doc classes inside this module.
# ``list_sent_drafts_for_followup`` (cross-tenant scan, the one deliberate
# global read), ``list_channel_drafts`` / ``get_prospect_system`` /
# ``mark_prospect_dead`` / ``create_followup_draft`` — all keyed on an explicit
# ``workspace_id`` because the sweep runs under the worker's system identity,
# mirroring ``upsert_by_domain`` and ``gate_transition``.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pydantic import ValidationError as PydanticValidationError

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.domain import (
    DRAFT_TRANSITIONS,
    GATE_OWNED_TARGETS,
    MESSAGE_LOG_OUTCOMES,
    Draft,
    MessageLog,
    Prospect,
)
from pocketpaw_ee.cloud.growth.dto import (
    BulkIngestRequest,
    BulkIngestResponse,
    BulkRowError,
    CreateDraftRequest,
    CreateProspectRequest,
    DraftResponse,
    LinkedInQueueItemResponse,
    ProposeSendResponse,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateProspectRequest,
)
from pocketpaw_ee.cloud.models.draft import Draft as _DraftDoc
from pocketpaw_ee.cloud.models.message_log import MessageLog as _MessageLogDoc
from pocketpaw_ee.cloud.models.prospect import Prospect as _ProspectDoc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _to_domain(doc: _ProspectDoc) -> Prospect:
    return Prospect(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        company=doc.company,
        domain=doc.domain,
        source=doc.source,
        tier=doc.tier,
        research_brief=doc.research_brief,
        emails=tuple(doc.emails),
        linkedin_url=doc.linkedin_url,
        whatsapp_number=doc.whatsapp_number,
        opted_in=doc.opted_in,
        status=doc.status,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


def _draft_to_domain(doc: _DraftDoc) -> Draft:
    return Draft(
        id=str(doc.id),
        workspace_id=doc.workspace,
        prospect_id=doc.prospect_id,
        channel=doc.channel,
        subject=doc.subject,
        body=doc.body,
        variant=doc.variant,
        status=doc.status,
        demo_url=doc.demo_url,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


def _draft_to_response(d: Draft) -> DraftResponse:
    return DraftResponse(
        id=d.id,
        workspace_id=d.workspace_id,
        prospect_id=d.prospect_id,
        channel=d.channel,
        subject=d.subject,
        body=d.body,
        variant=d.variant,
        status=d.status,
        demo_url=d.demo_url,
        created_at=iso_utc(d.created_at),
        updated_at=iso_utc(d.updated_at),
    )


def _to_response(p: Prospect) -> ProspectResponse:
    return ProspectResponse(
        id=p.id,
        workspace_id=p.workspace_id,
        name=p.name,
        company=p.company,
        domain=p.domain,
        source=p.source,
        tier=p.tier,
        research_brief=p.research_brief,
        emails=list(p.emails),
        linkedin_url=p.linkedin_url,
        whatsapp_number=p.whatsapp_number,
        opted_in=p.opted_in,
        status=p.status,
        created_at=iso_utc(p.created_at),
        updated_at=iso_utc(p.updated_at),
    )


# ---------------------------------------------------------------------------
# Tenancy helpers
# ---------------------------------------------------------------------------


def _require_workspace(ctx: RequestContext) -> str:
    """Growth always operates in a workspace; a route reached without an
    active workspace must fail closed, not fall through to a global read."""
    if not ctx.workspace_id:
        raise Forbidden("prospect.no_workspace", "Active workspace required for growth operations")
    return ctx.workspace_id


async def _fetch_in_workspace(workspace_id: str, prospect_id: str) -> _ProspectDoc:
    """Fetch a prospect scoped to the caller's workspace. Raises NotFound for
    a malformed id, a missing row, or a row in another workspace — identical
    404s, so existence never leaks across tenants."""
    try:
        oid = PydanticObjectId(prospect_id)
    except Exception as exc:  # noqa: BLE001 — malformed id == not found
        raise NotFound("prospect", prospect_id) from exc
    doc = await _ProspectDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("prospect", prospect_id)
    return doc


def _apply_update(doc: _ProspectDoc, body: UpdateProspectRequest) -> None:
    """Copy the non-None fields of a partial update onto the doc in place."""
    for field in (
        "name",
        "company",
        "tier",
        "research_brief",
        "emails",
        "linkedin_url",
        "whatsapp_number",
        "opted_in",
        "status",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(doc, field, value)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create(ctx: RequestContext, body: CreateProspectRequest) -> ProspectResponse:
    """Create a prospect. A duplicate (workspace, domain) is a 409 — callers
    that want create-or-update semantics use ``upsert_by_domain`` instead."""
    body = CreateProspectRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    existing = await _ProspectDoc.find_one({"workspace": workspace_id, "domain": body.domain})
    if existing is not None:
        raise ConflictError(
            "prospect.domain_taken",
            f"A prospect for domain '{body.domain}' already exists in this workspace",
        )

    doc = _ProspectDoc(workspace=workspace_id, **body.model_dump())
    await doc.insert()
    # no-event: growth has no realtime subscriber in v1; the prospects view polls.
    return _to_response(_to_domain(doc))


async def get(ctx: RequestContext, prospect_id: str) -> ProspectResponse:
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    return _to_response(_to_domain(doc))


async def list_prospects(
    ctx: RequestContext,
    *,
    tier: str | None = None,
    status: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[ProspectResponse]:
    """List the workspace's prospects, newest first, optionally filtered."""
    workspace_id = _require_workspace(ctx)
    filters: dict[str, Any] = {"workspace": workspace_id}
    if tier is not None:
        filters["tier"] = tier
    if status is not None:
        filters["status"] = status
    if source is not None:
        filters["source"] = source
    cursor = (
        _ProspectDoc.find(filters)
        .sort(-_ProspectDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    return [_to_response(_to_domain(doc)) async for doc in cursor]


async def update(
    ctx: RequestContext, prospect_id: str, body: UpdateProspectRequest
) -> ProspectResponse:
    body = UpdateProspectRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    _apply_update(doc, body)
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the prospects view polls.
    return _to_response(_to_domain(doc))


async def upsert_by_domain(
    workspace_id: str, prospect_data: CreateProspectRequest
) -> ProspectResponse:
    """Create-or-update keyed on (workspace_id, normalised domain).

    The ingestion seam later slices call: a re-imported company updates the
    existing row (never a duplicate); a new domain inserts. Takes an explicit
    ``workspace_id`` (not a RequestContext) because ingestion runs under a
    worker/system identity, mirroring how the arq worker trusts the doc's
    workspace. Every mutable field EXCEPT ``source`` is overwritten on update —
    source records provenance at first capture and is kept.
    """
    body = CreateProspectRequest.model_validate(prospect_data)

    doc = await _ProspectDoc.find_one({"workspace": workspace_id, "domain": body.domain})
    if doc is None:
        doc = _ProspectDoc(workspace=workspace_id, **body.model_dump())
        await doc.insert()
        # no-event: growth has no realtime subscriber in v1.
        return _to_response(_to_domain(doc))

    for field in (
        "name",
        "company",
        "tier",
        "research_brief",
        "emails",
        "linkedin_url",
        "whatsapp_number",
        "opted_in",
        "status",
    ):
        setattr(doc, field, getattr(body, field))
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1.
    return _to_response(_to_domain(doc))


async def bulk_ingest(ctx: RequestContext, body: BulkIngestRequest) -> BulkIngestResponse:
    """Ingest a batch of prospect rows via ``upsert_by_domain``.

    Each row is validated individually: an invalid row records a
    ``BulkRowError`` (with its payload index) and the remaining rows proceed —
    no all-or-nothing abort. Upserts are idempotent, so a partial failure
    needs no rollback and a re-run of the same payload is safe (second run
    reports every row as updated). The 500-row cap lives on the DTO, so an
    oversized payload 422s at the boundary before this function runs.
    """
    body = BulkIngestRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    created = 0
    updated = 0
    errors: list[BulkRowError] = []
    for index, raw in enumerate(body.rows):
        try:
            row = CreateProspectRequest.model_validate(raw)
        except PydanticValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first["loc"]) or "row"
            errors.append(
                BulkRowError(
                    index=index,
                    code="prospect.invalid_row",
                    message=f"{loc}: {first['msg']}",
                )
            )
            continue
        existing = await _ProspectDoc.find_one({"workspace": workspace_id, "domain": row.domain})
        await upsert_by_domain(workspace_id, row)
        if existing is None:
            created += 1
        else:
            updated += 1

    logger.info(
        "growth.bulk_ingest workspace=%s rows=%d created=%d updated=%d errors=%d",
        workspace_id,
        len(body.rows),
        created,
        updated,
        len(errors),
    )
    return BulkIngestResponse(created=created, updated=updated, errors=errors)


# ---------------------------------------------------------------------------
# Drafts (G-3)
# ---------------------------------------------------------------------------


async def _fetch_draft_in_workspace(workspace_id: str, draft_id: str) -> _DraftDoc:
    """Fetch a draft scoped to the caller's workspace — identical 404s for a
    malformed id, a missing row, or another tenant's row."""
    try:
        oid = PydanticObjectId(draft_id)
    except Exception as exc:  # noqa: BLE001 — malformed id == not found
        raise NotFound("draft", draft_id) from exc
    doc = await _DraftDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("draft", draft_id)
    return doc


async def create_draft(
    ctx: RequestContext, prospect_id: str, body: CreateDraftRequest
) -> DraftResponse:
    """Attach one channel's outreach copy to a prospect.

    The prospect must exist in the caller's workspace (cross-tenant ids 404,
    existence never leaks). A prospect still sitting in ``new`` / ``qualified``
    flips to ``drafted`` on its first draft; later statuses (``in_sequence``,
    ``replied``, ``dead``) are never regressed.
    """
    workspace_id = _require_workspace(ctx)
    return await _insert_draft(workspace_id, prospect_id, body)


async def _insert_draft(
    workspace_id: str, prospect_id: str, body: CreateDraftRequest
) -> DraftResponse:
    """Insert one draft against a prospect in ``workspace_id``.

    The shared core of the HTTP ``create_draft`` and the system-identity
    ``create_followup_draft`` (G-7) — same validation, same prospect check,
    same first-draft status nudge, so a follow-up born in the cron sweep is
    indistinguishable from one an operator typed.
    """
    body = CreateDraftRequest.model_validate(body)
    prospect = await _fetch_in_workspace(workspace_id, prospect_id)

    doc = _DraftDoc(
        workspace=workspace_id,
        prospect_id=str(prospect.id),
        **body.model_dump(),
    )
    await doc.insert()
    # no-event: growth has no realtime subscriber in v1; the drafts view polls.

    if prospect.status in ("new", "qualified"):
        prospect.status = "drafted"
        await prospect.save()  # bumps updatedAt
        # no-event: growth has no realtime subscriber in v1.

    return _draft_to_response(_draft_to_domain(doc))


async def list_drafts(
    ctx: RequestContext,
    *,
    prospect_id: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[DraftResponse]:
    """List the workspace's drafts, newest first, optionally filtered."""
    workspace_id = _require_workspace(ctx)
    filters: dict[str, Any] = {"workspace": workspace_id}
    if prospect_id is not None:
        filters["prospect_id"] = prospect_id
    if channel is not None:
        filters["channel"] = channel
    if status is not None:
        filters["status"] = status
    cursor = (
        _DraftDoc.find(filters)
        .sort(-_DraftDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    return [_draft_to_response(_draft_to_domain(doc)) async for doc in cursor]


async def transition(
    ctx: RequestContext, draft_id: str, body: TransitionDraftRequest
) -> DraftResponse:
    """Move a draft along the status machine — the PUBLIC route's enforcer.

    Legal moves per ``DRAFT_TRANSITIONS``: draft→proposed→approved→sent,
    sent→replied, any non-terminal→rejected. Anything else is a 422
    ``draft.illegal_transition``.

    G-4 — GATE-OWNED edges (``approved`` / ``sent``) are additionally refused
    here with 403 ``draft.gate_required`` even when legal per the table:
    ``approved`` is only reachable through an approved ``_growth_send``
    Instinct proposal (the growth executor's ``gate_transition`` call) and
    ``sent`` only through the dispatch worker. Structural, like /ship's
    destroy gate — no HTTP caller can approve or mark-sent a draft directly.
    """
    body = TransitionDraftRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_draft_in_workspace(workspace_id, draft_id)

    if body.status not in DRAFT_TRANSITIONS.get(doc.status, frozenset()):
        raise ValidationError(
            "draft.illegal_transition",
            f"Cannot move a draft from '{doc.status}' to '{body.status}'",
        )
    if body.status in GATE_OWNED_TARGETS:
        raise Forbidden(
            "draft.gate_required",
            f"'{body.status}' is set by the Instinct send gate — propose the draft "
            "and approve it in the Tray; it cannot be set directly",
        )

    doc.status = body.status
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the drafts view polls.
    return _draft_to_response(_draft_to_domain(doc))


async def gate_transition(workspace_id: str, draft_id: str, status: str) -> DraftResponse:
    """The Instinct-gate seam onto the draft status machine (G-4).

    Same legality table as ``transition`` (illegal moves still 422
    ``draft.illegal_transition``) but WITHOUT the public-route gate-owned
    restriction, and keyed on an explicit ``workspace_id`` instead of a
    RequestContext — the callers run under a system identity (the growth
    executor after an Instinct approval, the reject flip, and the G-5/G-6
    dispatch worker), mirroring how ``upsert_by_domain`` trusts the worker's
    workspace. NOT reachable from any HTTP route.
    """
    doc = await _fetch_draft_in_workspace(workspace_id, draft_id)
    if status not in DRAFT_TRANSITIONS.get(doc.status, frozenset()):
        raise ValidationError(
            "draft.illegal_transition",
            f"Cannot move a draft from '{doc.status}' to '{status}'",
        )
    doc.status = status
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the drafts view polls.
    return _draft_to_response(_draft_to_domain(doc))


async def propose_send(ctx: RequestContext, draft_id: str) -> ProposeSendResponse:
    """File a gated ``_growth_send`` Instinct proposal for a draft (G-4).

    Validates the draft can legally move to ``proposed`` (422 otherwise — so a
    second propose of the same draft is refused and no duplicate proposal is
    filed), loads the prospect for the Tray card, files the Instinct Action
    (the blob carries draft/prospect/channel + the rendered preview), then
    flips draft→proposed via the existing ``transition``. NOTHING sends here:
    the send is dispatched only by ``executor.execute_approved_growth_send``
    after a human approves the proposal.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_draft_in_workspace(workspace_id, draft_id)
    if "proposed" not in DRAFT_TRANSITIONS.get(doc.status, frozenset()):
        raise ValidationError(
            "draft.illegal_transition",
            f"Cannot move a draft from '{doc.status}' to 'proposed'",
        )
    prospect = await _fetch_in_workspace(workspace_id, doc.prospect_id)

    # Lazy import — keeps the service importable without the instinct stack
    # and mirrors the router's lazy-dispatch discipline.
    from pocketpaw_ee.cloud.growth.propose import propose_growth_send

    proposal_id = await propose_growth_send(
        workspace_id=workspace_id,
        draft_id=str(doc.id),
        prospect_id=doc.prospect_id,
        channel=doc.channel,
        prospect_name=prospect.name,
        prospect_company=prospect.company,
        preview_subject=doc.subject,
        preview_body=doc.body,
        requested_by=str(ctx.user_id or ""),
    )

    # The existing transition seam does the flip (draft→proposed is legal and
    # not gate-owned). Validated above, so this only races a concurrent move —
    # in which case the pending proposal stays for the human to reject.
    draft = await transition(ctx, draft_id, TransitionDraftRequest(status="proposed"))
    return ProposeSendResponse(proposal_id=proposal_id, draft=draft)


# ---------------------------------------------------------------------------
# LinkedIn manual queue (G-8)
# ---------------------------------------------------------------------------


async def linkedin_queue(
    ctx: RequestContext, *, limit: int = 100
) -> list[LinkedInQueueItemResponse]:
    """The manual LinkedIn send queue: the workspace's linkedin-channel drafts
    in ``proposed`` / ``approved``, newest first, each joined with its
    prospect's targeting context (name, company, profile URL, brief, tier).

    The join is two queries (drafts, then their prospects by id), not an
    aggregation — the queue is small (manual sending is the bottleneck by
    design). A draft whose prospect vanished is skipped rather than crashing
    the queue.
    """
    workspace_id = _require_workspace(ctx)
    cursor = (
        _DraftDoc.find(
            {
                "workspace": workspace_id,
                "channel": "linkedin",
                "status": {"$in": ["proposed", "approved"]},
            }
        )
        .sort(-_DraftDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    drafts = [_draft_to_domain(doc) async for doc in cursor]

    prospect_oids = []
    for draft in drafts:
        try:
            prospect_oids.append(PydanticObjectId(draft.prospect_id))
        except Exception:  # noqa: BLE001 — malformed ref == orphan, skipped below
            continue
    prospects: dict[str, Prospect] = {}
    if prospect_oids:
        async for pdoc in _ProspectDoc.find(
            {"workspace": workspace_id, "_id": {"$in": prospect_oids}}
        ):
            prospects[str(pdoc.id)] = _to_domain(pdoc)

    items: list[LinkedInQueueItemResponse] = []
    for draft in drafts:
        prospect = prospects.get(draft.prospect_id)
        if prospect is None:
            continue
        items.append(
            LinkedInQueueItemResponse(
                draft=_draft_to_response(draft),
                prospect_name=prospect.name,
                prospect_company=prospect.company,
                linkedin_url=prospect.linkedin_url,
                research_brief=prospect.research_brief,
                tier=prospect.tier,
            )
        )
    return items


def _one_line(text: str, max_len: int = 160) -> str:
    """First non-empty line of a blob, hard-capped for the one-line brief."""
    stripped = text.strip()
    line = stripped.splitlines()[0].strip() if stripped else ""
    return line if len(line) <= max_len else line[: max_len - 1] + "…"


def _render_queue_markdown(items: list[LinkedInQueueItemResponse]) -> str:
    """Render the queue as paste-ready markdown — one section per prospect.

    No tables, no HTML: heading = name + company, profile URL as a link, tier
    + one-line brief, then the connect note (first_touch body, with a char
    count against LinkedIn's 300-char connect limit) and the after-accept
    message (follow_up body, when one is queued), each with its draft id so
    mark-sent can be called after the manual send.
    """
    lines = ["# LinkedIn outreach queue", ""]
    if not items:
        lines.append("_Queue is empty — no proposed or approved LinkedIn drafts._")
        return "\n".join(lines) + "\n"

    grouped: dict[str, list[LinkedInQueueItemResponse]] = {}
    for item in items:  # preserves newest-first order of first appearance
        grouped.setdefault(item.draft.prospect_id, []).append(item)

    for group in grouped.values():
        head = group[0]
        lines.append(f"## {head.prospect_name} — {head.prospect_company}")
        lines.append("")
        if head.linkedin_url:
            lines.append(f"[LinkedIn profile]({head.linkedin_url})")
            lines.append("")
        brief = _one_line(head.research_brief)
        lines.append(f"Tier {head.tier.upper()}" + (f" — {brief}" if brief else ""))
        lines.append("")
        first = next((i for i in group if i.draft.variant == "first_touch"), None)
        follow = next((i for i in group if i.draft.variant == "follow_up"), None)
        for label, entry in (("Connect note", first), ("After accept", follow)):
            if entry is None:
                continue
            counter = f"{len(entry.draft.body)}/300 chars, " if label == "Connect note" else ""
            lines.append(f"{label} ({counter}{entry.draft.status}):")
            lines.append("")
            lines.append(entry.draft.body)
            lines.append("")
            lines.append(f"Draft id: `{entry.draft.id}`")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def linkedin_queue_markdown(ctx: RequestContext, *, limit: int = 100) -> str:
    """The queue as copy-paste markdown (``?format=md`` on the queue route)."""
    return _render_queue_markdown(await linkedin_queue(ctx, limit=limit))


async def mark_linkedin_sent(ctx: RequestContext, draft_id: str) -> DraftResponse:
    """Record that the captain manually sent a queued LinkedIn draft.

    Guard: the draft must be linkedin-channel (422 ``draft.wrong_channel``
    otherwise). The status move rides ``gate_transition``, not the public
    ``transition``: G-4 made ``sent`` a GATE_OWNED_TARGET that the public
    status route refuses with 403 ``draft.gate_required``, and this route IS
    the LinkedIn dispatch path — the "worker" is the human, because LinkedIn
    is manual by design. G-4's structural guarantee still holds: only
    ``approved`` can move to ``sent``, and ``approved`` is reachable only
    through an approved ``_growth_send`` proposal. The legality table is
    identical, so proposed→sent stays a 422 ``draft.illegal_transition``.
    The route sits at ``growth.manage`` — the same outbound tier as propose.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_draft_in_workspace(workspace_id, draft_id)
    if doc.channel != "linkedin":
        raise ValidationError(
            "draft.wrong_channel",
            f"mark-sent is for linkedin drafts; this draft targets '{doc.channel}'",
        )
    return await gate_transition(workspace_id, draft_id, "sent")


# ---------------------------------------------------------------------------
# Dispatch-worker seams (G-5)
# ---------------------------------------------------------------------------


async def get_draft_for_dispatch(draft_id: str) -> Draft | None:
    """Load a draft by id ALONE — the dispatch worker's entry read.

    The arq job carries only ``(draft_id, channel)``: the worker process has no
    RequestContext and no workspace to filter on, so tenancy is DERIVED from
    the row and every subsequent call (prospect read, status flip, audit write)
    is scoped to ``draft.workspace_id``. Safe because the id itself is not
    attacker-supplied — the only producer of this job is
    ``executor.execute_approved_growth_send``, which already validated the
    draft against the approved proposal's workspace. Returns ``None`` for a
    malformed or vanished id so the job can no-op instead of raising.

    # global-read: worker path — no request workspace exists to filter on; the
    # draft id comes from the gate's own enqueue and the row supplies tenancy.
    """
    try:
        oid = PydanticObjectId(draft_id)
    except Exception:  # noqa: BLE001 — malformed id == nothing to dispatch
        return None
    doc = await _DraftDoc.find_one({"_id": oid})
    return _draft_to_domain(doc) if doc is not None else None


async def get_prospect_for_dispatch(workspace_id: str, prospect_id: str) -> Prospect | None:
    """Load the draft's prospect for the dispatch worker, workspace-scoped.

    Takes the explicit ``workspace_id`` the draft supplied, so the read is
    tenant-filtered like every other prospect read. ``None`` (not NotFound) —
    a missing prospect is a recorded delivery failure, not an exception in a
    background job.
    """
    try:
        oid = PydanticObjectId(prospect_id)
    except Exception:  # noqa: BLE001 — malformed id == no prospect
        return None
    doc = await _ProspectDoc.find_one({"_id": oid, "workspace": workspace_id})
    return _to_domain(doc) if doc is not None else None


async def record_message_log(
    *,
    workspace_id: str,
    draft_id: str,
    prospect_id: str,
    channel: str,
    provider: str,
    to_address: str,
    outcome: str,
    provider_message_id: str | None = None,
    sent_at: datetime | None = None,
    error: str | None = None,
) -> MessageLog:
    """Write the audit row for ONE outbound delivery attempt (G-5).

    Sole writer of the ``MessageLog`` doc (the "Growth" import-linter contract
    keeps the doc class out of the worker/connector modules). One row per
    ATTEMPT: a ``failed`` row leaves the draft ``approved`` so the retry writes
    a second row and the delivery history stays complete.

    ``error`` is truncated — connectors already sanitise their messages, but a
    provider error body should never be able to bloat the audit collection.
    """
    if outcome not in MESSAGE_LOG_OUTCOMES:
        raise ValidationError(
            "message_log.invalid_outcome",
            f"'{outcome}' is not a delivery outcome ({sorted(MESSAGE_LOG_OUTCOMES)})",
        )
    if not workspace_id:
        raise ValidationError("message_log.no_workspace", "A message log needs a workspace")

    doc = _MessageLogDoc(
        workspace=workspace_id,
        draft_id=draft_id,
        prospect_id=prospect_id,
        channel=channel,
        provider=provider,
        provider_message_id=provider_message_id,
        to_address=to_address,
        sent_at=sent_at,
        outcome=outcome,
        error=error[:500] if error else None,
    )
    await doc.insert()
    # no-event: growth has no realtime subscriber in v1; the sends view polls.
    return MessageLog(
        id=str(doc.id),
        workspace_id=doc.workspace,
        draft_id=doc.draft_id,
        prospect_id=doc.prospect_id,
        channel=doc.channel,
        provider=doc.provider,
        to_address=doc.to_address,
        outcome=doc.outcome,
        provider_message_id=doc.provider_message_id,
        sent_at=doc.sent_at,
        error=doc.error,
        created_at=getattr(doc, "createdAt", None),
    )


# ---------------------------------------------------------------------------
# Follow-up sweep seams (G-7)
#
# The daily cron sweep (``growth/followups.py``) runs under the worker's system
# identity across every tenant, so these take an explicit ``workspace_id``
# instead of a RequestContext — same discipline as ``upsert_by_domain`` and
# ``gate_transition``. They live here (not in ``followups.py``) because the
# import-linter "Growth" contract keeps ``models.draft`` / ``models.prospect``
# behind this module.
# ---------------------------------------------------------------------------


def _resolved_sent_at(doc: _DraftDoc) -> datetime | None:
    """When this draft actually went out.

    Prefers an explicit ``sent_at`` written by the dispatch worker's send
    record (G-5/G-6) when the field exists, and otherwise falls back to
    ``updatedAt`` — which, for a draft sitting in ``sent``, IS the moment of
    the ``sent`` transition (the status flip is the last write). The fallback
    goes stale only if something else edits a sent draft, which no current
    code path does. Mongo returns naive datetimes, so anchor them to UTC.
    """
    raw = getattr(doc, "sent_at", None) or getattr(doc, "updatedAt", None)
    if raw is None:
        return None
    return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw


async def list_sent_drafts_for_followup(*, limit: int = 500) -> list[dict[str, Any]]:
    """Every draft currently sitting in ``sent``, oldest-touched first.

    global-read: the follow-up cron sweeps ALL tenants under the worker's
    system identity — there is no request workspace to filter on. Each row
    carries its own ``workspace_id`` and the caller re-enters per-workspace
    reads through the scoped helpers below, so nothing crosses tenants
    downstream.

    Returns lightweight wire dicts (not domain objects) so the sweep never
    needs the doc class. ``sent_at`` is resolved per ``_resolved_sent_at``;
    the caller applies the age threshold, keeping the delay policy in one
    place. The oldest-first sort means the ``limit`` cap sheds the NEWEST
    sends — the ones furthest from being due — when a backlog exceeds it.
    """
    cursor = (
        _DraftDoc.find({"status": "sent"})
        .sort(+_DraftDoc.updatedAt)  # type: ignore[operator]
        .limit(limit)
    )
    return [
        {
            "id": str(doc.id),
            "workspace_id": doc.workspace,
            "prospect_id": doc.prospect_id,
            "channel": doc.channel,
            "variant": doc.variant,
            "subject": doc.subject,
            "body": doc.body,
            "demo_url": doc.demo_url,
            "sent_at": _resolved_sent_at(doc),
        }
        async for doc in cursor
    ]


async def list_channel_drafts(
    workspace_id: str, prospect_id: str, channel: str
) -> list[dict[str, Any]]:
    """Every draft for one (prospect, channel) pair, oldest first.

    The sweep reads this to decide three things at once: is a follow-up
    already open (dedupe), how many follow-ups has this pair already had
    (the cap), and which draft was the first touch (the template source).
    """
    cursor = (
        _DraftDoc.find(
            {"workspace": workspace_id, "prospect_id": prospect_id, "channel": channel}
        ).sort(+_DraftDoc.createdAt)  # type: ignore[operator]
    )
    return [
        {
            "id": str(doc.id),
            "workspace_id": doc.workspace,
            "prospect_id": doc.prospect_id,
            "channel": doc.channel,
            "variant": doc.variant,
            "status": doc.status,
            "subject": doc.subject,
            "body": doc.body,
            "demo_url": doc.demo_url,
        }
        async for doc in cursor
    ]


async def get_prospect_system(workspace_id: str, prospect_id: str) -> ProspectResponse:
    """Read a prospect under the worker's system identity (still tenant-scoped:
    a cross-workspace id raises NotFound exactly as the HTTP ``get`` does)."""
    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    return _to_response(_to_domain(doc))


async def mark_prospect_dead(workspace_id: str, prospect_id: str) -> ProspectResponse:
    """Retire a prospect the sequence is done with (G-7 cap reached).

    ``dead`` is the terminal outbound status — the sweep skips those rows on
    every later pass, so nothing touches this prospect again. Idempotent: a
    prospect already ``dead`` is returned unchanged without a write.
    """
    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    if doc.status == "dead":
        return _to_response(_to_domain(doc))
    doc.status = "dead"
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the prospects view polls.
    return _to_response(_to_domain(doc))


async def create_followup_draft(
    workspace_id: str, prospect_id: str, body: CreateDraftRequest
) -> DraftResponse:
    """Create a follow-up draft under the worker's system identity (G-7).

    Same insert as the HTTP ``create_draft`` (shared ``_insert_draft`` core),
    keyed on an explicit workspace instead of a RequestContext. The draft is
    born in ``draft`` status like any other — the sweep then walks it onto the
    EXISTING gate propose path, so it reaches a human in The Tray and NOTHING
    is approved or sent without them.
    """
    body = CreateDraftRequest.model_validate(body)
    if body.variant != "follow_up":
        raise ValidationError(
            "draft.not_a_followup",
            "create_followup_draft only creates variant='follow_up' drafts",
        )
    return await _insert_draft(workspace_id, prospect_id, body)


__all__ = [
    "bulk_ingest",
    "create",
    "create_draft",
    "create_followup_draft",
    "gate_transition",
    "get",
    "get_draft_for_dispatch",
    "get_prospect_for_dispatch",
    "get_prospect_system",
    "linkedin_queue",
    "linkedin_queue_markdown",
    "list_channel_drafts",
    "list_drafts",
    "list_prospects",
    "list_sent_drafts_for_followup",
    "mark_linkedin_sent",
    "mark_prospect_dead",
    "propose_send",
    "record_message_log",
    "transition",
    "update",
    "upsert_by_domain",
]
