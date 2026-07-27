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
    Draft,
    Prospect,
)
from pocketpaw_ee.cloud.growth.dto import (
    BulkIngestRequest,
    BulkIngestResponse,
    BulkRowError,
    CreateDraftRequest,
    CreateProspectRequest,
    DraftResponse,
    ProposeSendResponse,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateProspectRequest,
)
from pocketpaw_ee.cloud.models.draft import Draft as _DraftDoc
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
    "get_prospect_system",
    "list_channel_drafts",
    "list_drafts",
    "list_prospects",
    "list_sent_drafts_for_followup",
    "mark_prospect_dead",
    "propose_send",
    "transition",
    "update",
    "upsert_by_domain",
]
