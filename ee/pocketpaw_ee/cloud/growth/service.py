# ee/pocketpaw_ee/cloud/growth/service.py — the /growth service and the SOLE
# owner of the Prospect, Icp, Draft and MessageLog doc writes (service-is-repo;
# the import-linter "Growth" contract keeps every other growth module, the
# worker and the agent MCP surface off the doc classes).
#
# Tenancy: every RequestContext read filters on ``workspace``, and malformed,
# missing and cross-tenant ids raise the same NotFound so existence never
# leaks. System seams (executor, dispatch worker, mock delivery, follow-up and
# discovery crons) take an explicit ``workspace_id``; the few deliberate
# cross-tenant reads carry ``global-read`` justifications.
#
# What it owns: prospect CRUD / bulk ingest / delete / research and the scale
# list (escaped ``q`` search, four sorts, keyset cursors, facet counts);
# ``upsert_by_domain`` (create-or-update on the normalised domain, set-only for
# project and provenance fields); ICP CRUD + preview; drafts and their status
# machine. ``transition`` is the public enforcer and refuses the gate-owned
# targets; ``gate_transition`` is the only way onto ``approved`` / ``sent``,
# used by the executor, the delivery paths and LinkedIn mark-sent.
# ``propose_send`` / ``propose_send_batch`` file one gated Instinct proposal
# per draft. Queues: the LinkedIn manual queue + markdown export, and
# ``delivery_queue`` (per channel, latest MessageLog row attached).
# ``deliver_approved`` and the growth settings drive mock delivery.
#
# MessageLog is the delivery audit and WhatsApp compliance record, one row per
# ATTEMPT (``record_message_log``, ``record_delivery_attempt`` /
# ``finish_delivery_attempt``). The WhatsApp hourly cap counts only rows that
# reached a real provider — never ``blocked`` rows, never mock ones. Writes carry
# ``# no-event:`` markers: growth has no realtime subscriber yet.

from __future__ import annotations

import inspect
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from beanie import PydanticObjectId
from pydantic import ValidationError as PydanticValidationError

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.domain import (
    DRAFT_TRANSITIONS,
    GATE_OWNED_TARGETS,
    MESSAGE_LOG_OUTCOMES,
    MOCK_DELIVERY_CHANNELS,
    MOCK_DELIVERY_PROVIDER,
    PROSPECT_SOURCE_ORDER,
    PROSPECT_STATUS_ORDER,
    PROVIDER_REACHED_OUTCOMES,
    TIER_SORT_ORDER,
    Draft,
    Icp,
    MessageLog,
    Prospect,
    recordable_emails,
    whatsapp_reply_intent,
)
from pocketpaw_ee.cloud.growth.dto import (
    BulkIngestRequest,
    BulkIngestResponse,
    BulkRowError,
    CreateDraftRequest,
    CreateIcpRequest,
    CreateProspectRequest,
    DeleteProspectsRequest,
    DeleteProspectsResponse,
    DeliverApprovedResponse,
    DeliveryQueueItemResponse,
    DeliveryStateResponse,
    DraftProspectRequest,
    DraftProspectResponse,
    DraftResponse,
    DraftSkipped,
    GrowthSettingsResponse,
    IcpLastPreviewResponse,
    IcpPreviewResponse,
    IcpResponse,
    LinkedInQueueItemResponse,
    PreviewedProspectResponse,
    ProposeBatchError,
    ProposeBatchRequest,
    ProposeBatchResponse,
    ProposeSendResponse,
    ProspectFacetsResponse,
    ProspectPageResponse,
    ProspectResearch,
    ProspectResponse,
    TransitionDraftRequest,
    UpdateDraftRequest,
    UpdateGrowthSettingsRequest,
    UpdateIcpRequest,
    UpdateProspectRequest,
    _normalise_domain,
)
from pocketpaw_ee.cloud.models.draft import Draft as _DraftDoc
from pocketpaw_ee.cloud.models.icp import Icp as _IcpDoc
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
        project_id=getattr(doc, "project_id", None),
        tier=doc.tier,
        research_brief=doc.research_brief,
        emails=tuple(doc.emails),
        linkedin_url=doc.linkedin_url,
        whatsapp_number=doc.whatsapp_number,
        opted_in=doc.opted_in,
        status=doc.status,
        icp_id=getattr(doc, "icp_id", None),
        source_urls=tuple(getattr(doc, "source_urls", None) or ()),
        research=getattr(doc, "research", None),
        researched_at=getattr(doc, "researched_at", None),
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
        project_id=p.project_id,
        tier=p.tier,
        research_brief=p.research_brief,
        emails=list(p.emails),
        linkedin_url=p.linkedin_url,
        whatsapp_number=p.whatsapp_number,
        opted_in=p.opted_in,
        status=p.status,
        icp_id=p.icp_id,
        source_urls=list(p.source_urls),
        research=ProspectResearch.model_validate(p.research) if p.research is not None else None,
        researched_at=iso_utc(p.researched_at),
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


async def _ensure_project_in_workspace(workspace_id: str, project_id: str) -> None:
    """Validate that ``project_id`` names a real project in this workspace.

    Growth's OWN copy of the check ``tasks`` and ``cycles`` each carry, rather
    than an import of ``tasks.service._ensure_project_in_workspace``: that one
    is private, and reaching across an entity boundary for a sibling's private
    helper is the drift the 4-file rule exists to prevent. Both copies call the
    same PUBLIC seam — ``projects.service.exists_in_workspace`` — so there is
    one source of truth for the answer, three callers for the question.

    The import is lazy for the same two reasons ``tasks`` gives: it avoids a
    module-load cycle, and it degrades silently on a build that predates the
    Projects entity (there, a supplied project id simply cannot be validated,
    and growth is not the module that should fail the deploy over it).
    """
    try:
        from pocketpaw_ee.cloud.projects import service as projects_service
    except Exception:  # noqa: BLE001 — no Projects entity on this build
        return
    if not await projects_service.exists_in_workspace(workspace_id, project_id):
        # Same 404 a foreign prospect id gets: another tenant's project must
        # not be distinguishable from one that never existed.
        raise NotFound("project", project_id)


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
    if body.project_id:
        await _ensure_project_in_workspace(workspace_id, body.project_id)

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


# ---------------------------------------------------------------------------
# Prospect list query (G-10a)
# ---------------------------------------------------------------------------

# Fields the ``q`` search scans. Deliberately the four a human types into a
# "find that company" box — identity (name/company), the dedupe key (domain),
# and the qualification notes (research_brief).
PROSPECT_SEARCH_FIELDS: tuple[str, ...] = ("name", "company", "domain", "research_brief")


def _escape_regex(term: str) -> str:
    """Neutralise regex metacharacters so a search term is matched literally.

    Without this a caller could submit ``.*`` (harmless but wrong results) or a
    catastrophic-backtracking pattern (a real DoS vector, since the regex runs
    server-side in Mongo). ``re.escape`` is the whole defence — the term is
    never compiled in Python, only handed to Mongo as a literal-ised pattern.
    """
    return re.escape(term.strip())


def _prospect_filters(
    workspace_id: str,
    *,
    tier: str | None = None,
    status: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Build the tenant-scoped Mongo filter shared by list / facets / count.

    ``project_id`` scopes to one client's pipeline. It is three-valued like
    ``tasks``: ``None`` is "every project" (the default — a workspace not using
    projects is unaffected), an id scopes to that client, and an empty string
    scopes to the UNASSIGNED rows, which is how the UI offers a "no client"
    bucket without a magic id.

    SCALE CEILING — ``q`` is an unanchored, case-insensitive regex ``$or``
    across four fields. Mongo cannot use an index for that, so it is a
    collection scan bounded by the ``workspace`` filter. That is fine at the
    single-workspace scale this surface targets (tens of thousands of rows,
    single-digit-millisecond scans); past ~100k prospects per workspace this
    needs a real text index or an external search index. A text index is NOT
    added here on purpose: ``models/prospect.py`` carries a unique
    (workspace, domain) index plus a (workspace, createdAt) list cursor, and a
    Mongo text index is a per-collection singleton that would have to be
    designed against those rather than bolted on.
    """
    filters: dict[str, Any] = {"workspace": workspace_id}
    if tier is not None:
        filters["tier"] = tier
    if status is not None:
        filters["status"] = status
    if source is not None:
        filters["source"] = source
    if project_id is not None:
        filters["project_id"] = project_id or None
    if q is not None and q.strip():
        pattern = _escape_regex(q)
        filters["$or"] = [
            {field: {"$regex": pattern, "$options": "i"}} for field in PROSPECT_SEARCH_FIELDS
        ]
    return filters


# Mongo sort keys per sort mode. Every spec ends in ``_id`` so ties break
# deterministically — without it two rows sharing a createdAt / company can
# swap places between two identical queries, which silently duplicates or drops
# a row across a paginated boundary. ``tier`` is absent on purpose: its order is
# a declared rank, not a field comparison — see ``_tier_ordered_page``.
_PROSPECT_SORT_SPECS: dict[str, list[tuple[str, int]]] = {
    "newest": [("createdAt", -1), ("_id", -1)],
    "oldest": [("createdAt", 1), ("_id", 1)],
    "company": [("company", 1), ("_id", 1)],
}


def _tier_buckets(filters: dict[str, Any]) -> tuple[str, ...]:
    """The tier buckets to walk, best-qualified first.

    An active ``tier`` filter collapses the walk to that one bucket — the
    filter is enum-validated at the router, so it is always a known tier.
    """
    active = filters.get("tier")
    if active is None:
        return TIER_SORT_ORDER
    return (active,) if active in TIER_SORT_ORDER else ()


async def _tier_ordered_page(
    filters: dict[str, Any],
    *,
    limit: int,
    start_tier: str | None = None,
    after_oid: PydanticObjectId | None = None,
) -> list[_ProspectDoc]:
    """Fetch up to ``limit`` docs ordered by the DECLARED tier rank.

    Mongo cannot sort by a rank that isn't in the document, and adding a
    computed rank via ``$addFields`` would drag the whole list query into an
    aggregation. Instead the rank is walked: one bounded query per tier bucket,
    in ``TIER_SORT_ORDER``, stopping as soon as the page is full — at most four
    queries, each bounded by the remaining page size.

    Within a bucket rows come back newest-first by ``_id`` (an ObjectId is
    monotonic in creation time, so it doubles as the recency key AND the
    tie-breaker, which keeps the resume key a single value).
    """
    collected: list[_ProspectDoc] = []
    buckets = _tier_buckets(filters)
    started = start_tier is None
    for bucket_tier in buckets:
        if not started:
            if bucket_tier != start_tier:
                continue
            started = True
        remaining = limit - len(collected)
        if remaining <= 0:
            break
        bucket_filter: dict[str, Any] = {**filters, "tier": bucket_tier}
        if bucket_tier == start_tier and after_oid is not None:
            bucket_filter["_id"] = {"$lt": after_oid}
        collected.extend(
            await _ProspectDoc.find(bucket_filter).sort([("_id", -1)]).limit(remaining).to_list()
        )
    return collected


# ---------------------------------------------------------------------------
# Keyset cursor (G-10a)
#
# Convention follows ``audit/service.py`` and ``sessions/service.py``: an
# opaque composite ``{sort_value}|{oid}``, keyset (not offset) so a page never
# skips or repeats a row when the collection is written to mid-scroll.
#
# ONE extension over those two: the sort mode is prefixed
# (``{sort}:{value}|{oid}``). Audit and sessions have a single fixed ordering,
# so their cursor can only ever be read back the way it was written. This list
# has four, and a UI that changes the sort while holding a cursor would
# otherwise resume against a key that means something different — silently
# wrong rows rather than an error. The prefix turns that into a 422.
#
# The oid is split off with ``rsplit`` because the company sort's value is
# user-supplied and may itself contain ``|``; an ObjectId hex never can.
# ---------------------------------------------------------------------------


def _encode_prospect_cursor(sort: str, value: str, oid: PydanticObjectId) -> str:
    return f"{sort}:{value}|{oid!s}"


def _decode_prospect_cursor(cursor: str, sort: str) -> tuple[str, PydanticObjectId]:
    """Split an opaque cursor into ``(sort_value, oid)``, verifying the mode.

    Any malformed cursor — bad shape, unparseable id, or a cursor minted under
    a different sort — is a 422 ``prospect.bad_cursor`` rather than a wrong
    page.
    """
    try:
        head, oid_str = cursor.rsplit("|", 1)
        mode, value = head.split(":", 1)
        # Broad catch: a bad id raises bson's InvalidId, not ValueError — same
        # reasoning as ``_fetch_in_workspace``, a malformed id is caller error.
        oid = PydanticObjectId(oid_str)
    except Exception as exc:  # noqa: BLE001 — any malformed cursor is a 422
        raise ValidationError("prospect.bad_cursor", "Invalid pagination cursor") from exc
    if mode != sort:
        raise ValidationError(
            "prospect.bad_cursor",
            f"Cursor was issued for sort '{mode}' but the request asks for '{sort}'",
        )
    return value, oid


def _keyset_clause(sort: str, value: str, oid: PydanticObjectId) -> dict[str, Any]:
    """The "strictly after the cursor row" clause for a non-tier sort."""
    if sort == "newest":
        at = _parse_cursor_datetime(value)
        return {"$or": [{"createdAt": {"$lt": at}}, {"createdAt": at, "_id": {"$lt": oid}}]}
    if sort == "oldest":
        at = _parse_cursor_datetime(value)
        return {"$or": [{"createdAt": {"$gt": at}}, {"createdAt": at, "_id": {"$gt": oid}}]}
    # company — ascending, ties broken by ascending _id
    return {"$or": [{"company": {"$gt": value}}, {"company": value, "_id": {"$gt": oid}}]}


def _parse_cursor_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("prospect.bad_cursor", "Invalid pagination cursor") from exc


def _cursor_value_for(doc: _ProspectDoc, sort: str) -> str:
    if sort == "company":
        return doc.company
    if sort == "tier":
        return doc.tier
    created = getattr(doc, "createdAt", None)
    return created.isoformat() if created is not None else ""


async def list_prospects(
    ctx: RequestContext,
    *,
    tier: str | None = None,
    status: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
    sort: str = "newest",
    cursor: str | None = None,
    limit: int = 100,
) -> ProspectPageResponse:
    """One page of the workspace's prospects, filtered and ordered.

    ``q`` is a case-insensitive substring match across name / company /
    domain / research_brief (see ``_prospect_filters`` for the scale ceiling).
    ``sort`` is ``newest`` (default, the pre-G-10a ordering) / ``oldest`` /
    ``company`` / ``tier``. ``cursor`` resumes after the last row of the
    previous page — pass back the ``next_cursor`` the page returned, unchanged.

    ``total`` counts every row matching the filters, NOT the page, and is
    computed without the cursor clause so it stays put while the caller pages
    through ("showing 40 of 3,182").
    """
    workspace_id = _require_workspace(ctx)
    if sort != "tier" and sort not in _PROSPECT_SORT_SPECS:
        raise ValidationError("prospect.bad_sort", f"Unknown sort mode '{sort}'")
    filters = _prospect_filters(
        workspace_id, tier=tier, status=status, source=source, project_id=project_id, q=q
    )

    # Over-fetch by one: the extra row is the "is there a next page" probe and
    # is never returned.
    probe = limit + 1
    if sort == "tier":
        start_tier: str | None = None
        after_oid: PydanticObjectId | None = None
        if cursor:
            start_tier, after_oid = _decode_prospect_cursor(cursor, sort)
            if start_tier not in TIER_SORT_ORDER:
                raise ValidationError("prospect.bad_cursor", "Invalid pagination cursor")
        docs = await _tier_ordered_page(
            filters, limit=probe, start_tier=start_tier, after_oid=after_oid
        )
    else:
        find_filter: dict[str, Any] = filters
        if cursor:
            value, oid = _decode_prospect_cursor(cursor, sort)
            find_filter = {"$and": [filters, _keyset_clause(sort, value, oid)]}
        docs = (
            await _ProspectDoc.find(find_filter)
            .sort(_PROSPECT_SORT_SPECS[sort])
            .limit(probe)
            .to_list()
        )

    has_more = len(docs) > limit
    page = docs[:limit]
    next_cursor = (
        _encode_prospect_cursor(sort, _cursor_value_for(page[-1], sort), page[-1].id)
        if has_more and page
        else None
    )
    total = await _ProspectDoc.find(filters).count()
    return ProspectPageResponse(
        items=[_to_response(_to_domain(doc)) for doc in page],
        next_cursor=next_cursor,
        total=total,
    )


# The facet blocks: response field → (document field, display order).
_PROSPECT_FACETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tier": ("tier", TIER_SORT_ORDER),
    "status": ("status", PROSPECT_STATUS_ORDER),
    "source": ("source", PROSPECT_SOURCE_ORDER),
}


async def prospect_facets(
    ctx: RequestContext,
    *,
    tier: str | None = None,
    status: str | None = None,
    source: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
) -> ProspectFacetsResponse:
    """Counts per tier / status / source for the filter chips.

    Each block respects every active filter EXCEPT its own: with a status
    filter on, the tier counts still describe that status's rows rather than
    collapsing to the one selected tier. That self-exclusion is what makes a
    facet a facet — otherwise the selected chip reads ``n`` and every sibling
    reads ``0``, which tells the user nothing about where to go next.

    ONE aggregation, workspace-scoped: the ``$or`` search and the tenancy
    filter (which every block shares) go in the outer ``$match``, and each
    ``$facet`` branch adds only the sibling filters it needs. Three round
    trips would be three chances for the counts to disagree with each other.
    """
    workspace_id = _require_workspace(ctx)
    active = {"tier": tier, "status": status, "source": source}

    # Shared prefix — tenancy, the search term, and the project scope. The
    # project goes in the OUTER match rather than getting a facet block of its
    # own: it is not a chip the user toggles inside the list, it is which
    # client's list they are looking at. Counts for the other three must be
    # scoped to that client or the chips describe a pipeline nobody is viewing.
    outer = _prospect_filters(workspace_id, project_id=project_id, q=q)

    branches: dict[str, list[dict[str, Any]]] = {}
    for block, (field, _order) in _PROSPECT_FACETS.items():
        siblings = {f: v for f, v in active.items() if f != block and v is not None}
        stages: list[dict[str, Any]] = []
        if siblings:
            stages.append({"$match": siblings})
        stages.append({"$group": {"_id": f"${field}", "n": {"$sum": 1}}})
        branches[block] = stages

    pipeline: list[dict[str, Any]] = [{"$match": outer}, {"$facet": branches}]
    # Straight to the driver collection rather than Beanie's
    # ``Document.aggregate``, whose internal ``await`` breaks under the
    # mongomock-motor harness. The async PyMongo driver's ``aggregate()`` is a
    # coroutine resolving to the cursor while mongomock returns the cursor
    # directly, so ``inspect.isawaitable`` discriminates (the same idiom as
    # ``storage/service.py``); ``to_list`` then works on either cursor.
    cursor = _ProspectDoc.get_pymongo_collection().aggregate(pipeline)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    rows = await cursor.to_list(None)
    raw: dict[str, Any] = rows[0] if rows else {}

    counted: dict[str, dict[str, int]] = {}
    for block, (_field, order) in _PROSPECT_FACETS.items():
        seen = {b["_id"]: int(b["n"]) for b in raw.get(block, []) if b.get("_id") is not None}
        # Every legal value is present, zeros included, so the chip row keeps a
        # stable shape while the user filters. An unknown value (legacy row,
        # hand-edited document) is appended rather than silently dropped.
        counted[block] = {value: seen.pop(value, 0) for value in order} | {
            value: count for value, count in seen.items()
        }
    return ProspectFacetsResponse(**counted)


async def update(
    ctx: RequestContext, prospect_id: str, body: UpdateProspectRequest
) -> ProspectResponse:
    body = UpdateProspectRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    if body.project_id is not None:
        # Three-valued, like ``tasks``: an id reassigns (validated first), an
        # empty string clears. ``None`` never reaches here.
        if body.project_id:
            await _ensure_project_in_workspace(workspace_id, body.project_id)
            doc.project_id = body.project_id
        else:
            doc.project_id = None
    _apply_update(doc, body)
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the prospects view polls.
    return _to_response(_to_domain(doc))


async def delete_prospects(ctx: RequestContext, ids: list[str]) -> DeleteProspectsResponse:
    """Delete prospects in the caller's workspace, with every draft on them.

    Malformed, unknown and other-workspace ids are skipped, so the counts are
    the only record of what was removed. Pending Instinct proposals for
    ``proposed`` drafts are withdrawn best-effort. Message-log rows stay: they
    are the audit record of what was sent.
    """
    ids = DeleteProspectsRequest(ids=list(ids)).ids
    workspace_id = _require_workspace(ctx)

    oids: list[PydanticObjectId] = []
    for raw in ids:
        try:
            oids.append(PydanticObjectId(raw))
        except Exception:
            continue
    if not oids:
        return DeleteProspectsResponse(deleted=0, drafts_removed=0, proposals_withdrawn=0)

    prospects = await _ProspectDoc.find({"_id": {"$in": oids}, "workspace": workspace_id}).to_list()
    if not prospects:
        return DeleteProspectsResponse(deleted=0, drafts_removed=0, proposals_withdrawn=0)
    prospect_ids = [str(p.id) for p in prospects]

    drafts = await _DraftDoc.find(
        {"workspace": workspace_id, "prospect_id": {"$in": prospect_ids}}
    ).to_list()

    withdrawn = 0
    if any(d.status == "proposed" for d in drafts):
        from pocketpaw_ee.cloud.growth.propose import withdraw_growth_proposals

        withdrawn = await withdraw_growth_proposals(
            workspace_id=workspace_id,
            draft_ids={str(d.id) for d in drafts},
            rejector=str(ctx.user_id or "system"),
        )

    if drafts:
        await _DraftDoc.find(
            {"_id": {"$in": [d.id for d in drafts]}, "workspace": workspace_id}
        ).delete()
    await _ProspectDoc.find(
        {"_id": {"$in": [p.id for p in prospects]}, "workspace": workspace_id}
    ).delete()
    logger.info(
        "growth.delete_prospects workspace=%s prospects=%d drafts=%d proposals_withdrawn=%d",
        workspace_id,
        len(prospects),
        len(drafts),
        withdrawn,
    )
    # no-event: growth has no realtime subscriber in v1; the prospects view polls.
    return DeleteProspectsResponse(
        deleted=len(prospects), drafts_removed=len(drafts), proposals_withdrawn=withdrawn
    )


async def delete_prospect(ctx: RequestContext, prospect_id: str) -> None:
    """Delete one prospect through ``delete_prospects``. 404 for a malformed,
    missing or other-workspace id, the same answer GET gives."""
    workspace_id = _require_workspace(ctx)
    await _fetch_in_workspace(workspace_id, prospect_id)
    await delete_prospects(ctx, [prospect_id])
    # no-event: growth has no realtime subscriber in v1; the prospects view polls.


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

    ``project_id`` is one of the overwritten fields, and it is validated
    against ``workspace_id`` BEFORE anything is written: an ingestion path is
    exactly where a bad id would otherwise get in unchecked.
    """
    body = CreateProspectRequest.model_validate(prospect_data)
    if body.project_id:
        await _ensure_project_in_workspace(workspace_id, body.project_id)

    doc = await _ProspectDoc.find_one({"workspace": workspace_id, "domain": body.domain})
    if doc is None:
        doc = _ProspectDoc(workspace=workspace_id, **body.model_dump())
        await doc.insert()
        # no-event: growth has no realtime subscriber in v1.
        return _to_response(_to_domain(doc))

    # Descriptive fields a re-import may OVERWRITE. Everything omitted from
    # this tuple is deliberate, not forgotten.
    for field in ("name", "company", "tier", "research_brief"):
        setattr(doc, field, getattr(body, field))

    # LIFECYCLE IS NOT IMPORT DATA. ``status`` and ``opted_in`` are never
    # written here at all — an import describes who someone is, it does not
    # decide where they sit in a sequence or whether they consented. Both move
    # only through PATCH (an explicit human act) or the gate.
    #
    # A first attempt at this let a non-default status through
    # (``if body.status != "new"``), which protected only a sheet with no
    # status column — and an operator's working sheet is exactly the one that
    # carries a stale ``in_sequence``. Re-uploading it lifted rows back out of
    # the terminal set and the follow-up sweep re-entered the sequence on
    # people it had retired, including anyone marked dead for asking not to be
    # contacted.

    # THE NUMBER AND THE CONSENT MOVE TOGETHER, and this is the one that
    # actually reaches a stranger. Consent is given by a PERSON on a NUMBER,
    # never by a domain: if a re-import points the row at a different number,
    # whatever opt-in was recorded belonged to whoever held the old one.
    #
    # An earlier version of this fix overwrote ``whatsapp_number``
    # unconditionally while making ``opted_in`` sticky-True — so a sheet
    # carrying a new SDR's mobile and no opt-in column inherited the founder's
    # consent, cleared the dispatch guard, and put a business-initiated
    # template in front of someone who never agreed to it. The blanket
    # overwrite it replaced did NOT have that hole, because it reset
    # ``opted_in`` on the same import. A fix must not be more dangerous than
    # the bug.
    incoming_number = body.whatsapp_number
    if incoming_number is not None and incoming_number != doc.whatsapp_number:
        doc.whatsapp_number = incoming_number
        doc.opted_in = False

    # ``emails`` / ``linkedin_url`` are set-only for the same reason
    # ``project_id`` is below: a row that says nothing about them must not
    # erase enrichment a later pass (or a human) added.
    if body.emails:
        doc.emails = body.emails
    if body.linkedin_url:
        doc.linkedin_url = body.linkedin_url
    # ``project_id`` is one of the fields an upsert only ever SETS, never
    # clears. A re-import that names a project reassigns the row; one that says
    # nothing leaves the assignment alone. The alternative — treating the DTO's
    # default ``None`` as "unassign" — would make every agent enrichment call
    # (``growth_upsert_prospect``, which carries no project) silently orphan a
    # client's prospect. Un-assigning is an explicit act: PATCH with ``""``.
    if body.project_id:
        doc.project_id = body.project_id
    # Discovery provenance follows the same set-only rule, for the same reason
    # doubled: an enrichment call that carries no ICP and no source URLs must
    # not ERASE the record of where a discovered row came from. Provenance that
    # a later write can silently delete is not provenance.
    if body.icp_id:
        doc.icp_id = body.icp_id
    if body.source_urls:
        doc.source_urls = list(body.source_urls)
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
        try:
            await upsert_by_domain(workspace_id, row)
        except CloudError as exc:
            # A row can be well-FORMED and still be refused — naming a project
            # that isn't in this workspace is the case that brought this here.
            # It is one bad row, not a bad payload, so it joins the error list
            # like a malformed one instead of aborting the other 499.
            errors.append(BulkRowError(index=index, code=exc.code, message=exc.message))
            continue
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
# ICPs (feat/growth-discovery)
# ---------------------------------------------------------------------------


def _icp_to_domain(doc: _IcpDoc) -> Icp:
    return Icp(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        criteria=doc.criteria,
        project_id=doc.project_id,
        geography=doc.geography,
        exclusions=doc.exclusions,
        cadence=doc.cadence,
        max_per_run=doc.max_per_run,
        status=doc.status,
        last_run_at=doc.last_run_at,
        last_preview=doc.last_preview,
        last_preview_at=doc.last_preview_at,
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


def _icp_to_response(icp: Icp) -> IcpResponse:
    return IcpResponse(
        id=icp.id,
        workspace_id=icp.workspace_id,
        name=icp.name,
        criteria=icp.criteria,
        project_id=icp.project_id,
        geography=icp.geography,
        exclusions=icp.exclusions,
        cadence=icp.cadence,
        max_per_run=icp.max_per_run,
        status=icp.status,
        last_run_at=iso_utc(icp.last_run_at),
        last_preview=(
            IcpLastPreviewResponse.model_validate(icp.last_preview)
            if icp.last_preview is not None
            else None
        ),
        last_preview_at=iso_utc(icp.last_preview_at),
        created_at=iso_utc(icp.created_at),
        updated_at=iso_utc(icp.updated_at),
    )


_ICP_RESEARCH_INPUTS = ("criteria", "geography", "exclusions", "max_per_run")


def _icp_research_inputs(doc: _IcpDoc) -> dict[str, Any]:
    """The fields a preview was run against. A stored preview vouches for
    exactly these values and no others."""
    return {field: getattr(doc, field) for field in _ICP_RESEARCH_INPUTS}


async def _fetch_icp_in_workspace(workspace_id: str, icp_id: str) -> _IcpDoc:
    """Fetch an ICP scoped to the caller's workspace — identical 404s for a
    malformed id, a missing row, or another tenant's row, so existence never
    leaks (same shape as ``_fetch_in_workspace``)."""
    try:
        oid = PydanticObjectId(icp_id)
    except Exception as exc:  # noqa: BLE001 — malformed id == not found
        raise NotFound("icp", icp_id) from exc
    doc = await _IcpDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("icp", icp_id)
    return doc


async def create_icp(ctx: RequestContext, body: CreateIcpRequest) -> IcpResponse:
    """Create an ICP. No uniqueness constraint: two ICPs may share a name (an
    agency running "dental clinics" for two clients holds two of them, told
    apart by ``project_id``), so there is no 409 here."""
    body = CreateIcpRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    if body.project_id:
        await _ensure_project_in_workspace(workspace_id, body.project_id)

    doc = _IcpDoc(workspace=workspace_id, **body.model_dump())
    await doc.insert()
    # no-event: growth has no realtime subscriber in v1; the ICP view polls.
    return _icp_to_response(_icp_to_domain(doc))


async def get_icp(ctx: RequestContext, icp_id: str) -> IcpResponse:
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_icp_in_workspace(workspace_id, icp_id)
    return _icp_to_response(_icp_to_domain(doc))


async def list_icps(
    ctx: RequestContext,
    *,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[IcpResponse]:
    """The workspace's ICPs, newest first.

    A bare list rather than the prospect list's page envelope: an ICP is a
    hand-written artifact and a workspace holds a handful, not thousands. If
    that ever stops being true the answer is the same cursor machinery the
    prospect list already carries, not a bigger limit.

    ``project_id`` is three-valued like the prospect list — omitted means every
    project, an id scopes to that client, ``""`` selects the unassigned ones.
    """
    workspace_id = _require_workspace(ctx)
    filters: dict[str, Any] = {"workspace": workspace_id}
    if project_id is not None:
        filters["project_id"] = project_id or None
    if status is not None:
        filters["status"] = status
    docs = await _IcpDoc.find(filters).sort([("createdAt", -1), ("_id", -1)]).limit(limit).to_list()
    return [_icp_to_response(_icp_to_domain(doc)) for doc in docs]


async def update_icp(ctx: RequestContext, icp_id: str, body: UpdateIcpRequest) -> IcpResponse:
    """Partial update. Switching ``cadence`` on is how discovery starts running
    on a schedule — the field is an ordinary edit here because the BOUNDS (per
    run and per workspace per month) are what make an always-on cadence safe,
    not a second approval on the switch.

    An edit that actually changes what the research reads (``criteria``,
    ``geography``, ``exclusions``, ``max_per_run``) clears ``last_preview``:
    the UI unlocks the cadence switch off a stored preview, so one must never
    vouch for criteria nobody previewed. Renaming, re-scheduling, pausing or
    reassigning the project keeps it, and so does re-sending an unchanged
    value."""
    body = UpdateIcpRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_icp_in_workspace(workspace_id, icp_id)

    if body.project_id is not None:
        if body.project_id:
            await _ensure_project_in_workspace(workspace_id, body.project_id)
            doc.project_id = body.project_id
        else:
            doc.project_id = None
    researched = _icp_research_inputs(doc)
    for field in (
        "name",
        "criteria",
        "geography",
        "exclusions",
        "cadence",
        "max_per_run",
        "status",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(doc, field, value)
    if _icp_research_inputs(doc) != researched:
        doc.last_preview = None
        doc.last_preview_at = None
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the ICP view polls.
    return _icp_to_response(_icp_to_domain(doc))


async def preview_icp(ctx: RequestContext, icp_id: str) -> IcpPreviewResponse:
    """Dry-run an ICP: research once and report what WOULD be filed. Writes no
    prospects; records the result on the ICP as its last preview.

    Lives here rather than in the router because tenancy does — the router
    stays a thin shell and every workspace check in this entity is in one file.
    The actual work is ``discovery.preview_discovery``; the import is lazy
    because discovery imports this module back (it may only reach docs through
    these seams), and a lazy import is how the followups cron already resolves
    the same loop.

    A deployment with no research backend wired returns 503 rather than an
    empty preview. "Found nobody" and "nothing went looking" are different
    answers, and an operator tuning criteria against a silently-disabled engine
    would rewrite them forever. That path records nothing.

    The result — a failed attempt included, ``error`` set — is stored as
    ``last_preview`` / ``last_preview_at`` so a page refresh does not lose a
    research pass someone already paid for. The write is conditional on the
    research inputs still matching what was previewed: an edit that lands
    while the research is running wins, and the stale result is dropped
    rather than left vouching for criteria nobody previewed. Recording is
    best-effort; the caller gets the preview either way.
    """
    from pocketpaw_ee.cloud.growth import discovery as growth_discovery

    workspace_id = _require_workspace(ctx)
    research_fn = growth_discovery.resolve_research_fn()
    if research_fn is None:
        raise CloudError(
            503,
            "icp.research_unavailable",
            "Discovery research is not configured on this deployment",
        )

    previewed = _icp_research_inputs(await _fetch_icp_in_workspace(workspace_id, icp_id))
    preview = await growth_discovery.preview_discovery(workspace_id, icp_id, research_fn)
    response = IcpPreviewResponse(
        icp_id=preview.icp_id,
        items=[
            PreviewedProspectResponse(
                domain=item.domain,
                name=item.name,
                company=item.company,
                research_brief=item.research_brief,
                source_urls=list(item.source_urls),
                emails=list(item.emails),
                already_known=item.already_known,
            )
            for item in preview.items
        ],
        notes=preview.notes,
        error=preview.error,
    )
    await _record_last_preview(workspace_id, icp_id, previewed, response)
    return response


async def _record_last_preview(
    workspace_id: str,
    icp_id: str,
    previewed: dict[str, Any],
    response: IcpPreviewResponse,
) -> None:
    """Store a preview on its ICP, only if the ICP still has the research
    inputs the preview ran against. One atomic conditional ``$set``, so a
    concurrent edit either lands first (and the stale preview is dropped) or
    after (and clears it)."""
    now = datetime.now(UTC)
    try:
        await _IcpDoc.find_one(
            {"_id": PydanticObjectId(icp_id), "workspace": workspace_id, **previewed}
        ).update(
            {
                "$set": {
                    "last_preview": response.model_dump(exclude={"icp_id"}),
                    "last_preview_at": now,
                    "updatedAt": now,
                }
            }
        )
    except Exception:  # noqa: BLE001
        logger.warning("growth: could not record last preview for icp=%s", icp_id, exc_info=True)
    # no-event: growth has no realtime subscriber in v1; the ICP view polls.


async def delete_icp(ctx: RequestContext, icp_id: str) -> None:
    """Delete an ICP.

    Prospects it discovered are LEFT ALONE, ``icp_id`` and all. Provenance is a
    record of what happened, not a foreign key: those rows really were found by
    a profile that really existed, and deleting the definition does not un-find
    them. The alternative — nulling the pointer across every discovered row —
    is a mass write that erases the only answer to "where did this company come
    from" for rows a human is about to review.

    An operator who wants the ICP to stop running without losing the definition
    pauses it instead (``status="paused"``).
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_icp_in_workspace(workspace_id, icp_id)
    await doc.delete()
    # no-event: growth has no realtime subscriber in v1; the ICP view polls.


# ---------------------------------------------------------------------------
# Single-prospect actions: research one prospect, draft its first touch
# ---------------------------------------------------------------------------

_SOURCE_URL_CAP = 50
_ALL_DRAFT_CHANNELS = ("email", "linkedin", "whatsapp")
_LIVE_DRAFT_STATUSES = ("draft", "proposed", "approved", "sent")


async def _icp_for(workspace_id: str, doc: _ProspectDoc) -> Icp | None:
    """The prospect's ICP as context for an agent run. A deleted or foreign
    ICP is simply no context, never a 404 on the prospect."""
    icp_id = getattr(doc, "icp_id", None)
    if not icp_id:
        return None
    try:
        return _icp_to_domain(await _fetch_icp_in_workspace(workspace_id, icp_id))
    except NotFound:
        return None


def _linkedin_url_or_none(value: str) -> str | None:
    """``value`` if it is an https URL on linkedin.com, else None. Hostname is
    what the browser would connect to, so ``https://linkedin.com@evil.io`` is
    evil.io and is refused."""
    value = value.strip()
    if not value or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    return value


def _is_http_url(value: str) -> bool:
    try:
        return urlsplit(value).scheme in ("http", "https")
    except ValueError:
        return False


def _research_brief_from(profile: ProspectResearch) -> str:
    parts = [profile.summary]
    if profile.fit:
        parts.append(f"Fit: {profile.fit}")
    if profile.hook:
        parts.append(f"Hook: {profile.hook}")
    return "\n\n".join(p for p in parts if p)


def _apply_research(doc: _ProspectDoc, company: Any, now: datetime) -> None:
    """Fold one research result into the prospect.

    Fills gaps and never overwrites what a person typed: name and company only
    when blank, emails and sources merged, LinkedIn only when blank. Emails
    come from the evidence through ``recordable_emails`` and nowhere else.
    Tier is suggested only to an unqualified prospect, and status moves
    new → qualified and never backwards.
    """
    profile = ProspectResearch.model_validate(company.profile or {})

    if not doc.name.strip() and company.name:
        doc.name = company.name[:200]
    if not doc.company.strip() and company.company:
        doc.company = company.company[:200]

    emails = list(doc.emails)
    known = {e.lower() for e in emails}
    for address in recordable_emails(company.emails):
        if address not in known:
            emails.append(address)
            known.add(address)
    doc.emails = emails

    if not (doc.linkedin_url or "").strip():
        linkedin = _linkedin_url_or_none(company.linkedin_url)
        if linkedin is not None:
            doc.linkedin_url = linkedin

    brief = _research_brief_from(profile) or company.research_brief.strip()
    if brief:
        doc.research_brief = brief

    sources = list(getattr(doc, "source_urls", None) or [])
    for url in (*company.source_urls, *profile.sources):
        if len(sources) >= _SOURCE_URL_CAP:
            break
        if url not in sources and _is_http_url(url):
            sources.append(url)
    doc.source_urls = sources

    if doc.tier == "unqualified" and profile.suggested_tier:
        doc.tier = profile.suggested_tier
    if doc.status == "new":
        doc.status = "qualified"

    doc.research = profile.model_dump()
    doc.researched_at = now


async def research_prospect(ctx: RequestContext, prospect_id: str) -> ProspectResponse:
    """Research one prospect with the researcher agent and fold the result in.

    503 when no backend is wired (nothing went looking is not the same answer
    as found nothing); 502 when the run fails or returns no entry for this
    prospect's domain. The row is re-read after the run, so an edit made while
    the agent was working is kept rather than overwritten by a stale copy.
    """
    from pocketpaw_ee.cloud.growth import researcher as growth_researcher

    workspace_id = _require_workspace(ctx)
    research_fn = growth_researcher.resolve_prospect_research_fn()
    if research_fn is None:
        raise CloudError(
            503,
            "prospect.research_unavailable",
            "Prospect research is not configured on this deployment",
        )

    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    icp = await _icp_for(workspace_id, doc)
    try:
        outcome = await research_fn(_to_domain(doc), icp)
    except CloudError:
        raise
    except growth_researcher.ResearchUnavailable as exc:
        raise CloudError(502, "prospect.research_failed", f"Research failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("growth: research run failed for prospect=%s", prospect_id, exc_info=True)
        raise CloudError(502, "prospect.research_failed", "The research run failed") from exc

    if outcome.company is None:
        raise CloudError(
            502,
            "prospect.research_failed",
            "The researcher returned nothing usable for this prospect",
        )

    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    _apply_research(doc, outcome.company, datetime.now(UTC))
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the prospect view polls.
    return _to_response(_to_domain(doc))


def _channel_ineligible_reason(doc: _ProspectDoc, channel: str) -> str | None:
    if channel == "email" and not doc.emails:
        return "no email address on file"
    if channel == "linkedin" and not (doc.linkedin_url or "").strip():
        return "no LinkedIn profile on file"
    if channel == "whatsapp":
        if not (doc.whatsapp_number or "").strip():
            return "no WhatsApp number on file"
        if not doc.opted_in:
            return "the prospect has not opted in to WhatsApp"
    return None


async def _first_touch_channels(workspace_id: str, prospect_id: str) -> set[str]:
    docs = await _DraftDoc.find(
        {
            "workspace": workspace_id,
            "prospect_id": prospect_id,
            "variant": "first_touch",
            "status": {"$in": list(_LIVE_DRAFT_STATUSES)},
        }
    ).to_list()
    return {d.channel for d in docs}


async def draft_prospect(
    ctx: RequestContext, prospect_id: str, body: DraftProspectRequest
) -> DraftProspectResponse:
    """Have the writer agent draft first-touch copy for a prospect.

    Only channels the prospect can actually be reached on are written for
    (email needs an address, LinkedIn a profile URL, WhatsApp a number AND an
    opt-in), and a channel that already holds a live first-touch draft is
    skipped rather than duplicated. Every skipped channel comes back with its
    reason. Anything the writer returns for a channel it was not given is
    dropped. Drafts go through ``_insert_draft``, so they are born ``draft``
    and the prospect moves to ``drafted`` exactly as a hand-typed draft would.
    """
    from pocketpaw_ee.cloud.growth import writer as growth_writer
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable

    body = DraftProspectRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    writer_fn = growth_writer.resolve_writer_fn()
    if writer_fn is None:
        raise CloudError(
            503,
            "prospect.writer_unavailable",
            "Draft writing is not configured on this deployment",
        )

    doc = await _fetch_in_workspace(workspace_id, prospect_id)
    requested = list(
        dict.fromkeys(body.channels if body.channels is not None else _ALL_DRAFT_CHANNELS)
    )
    drafted = await _first_touch_channels(workspace_id, str(doc.id))
    eligible: list[str] = []
    skipped: list[DraftSkipped] = []
    for channel in requested:
        reason = (
            "already drafted" if channel in drafted else _channel_ineligible_reason(doc, channel)
        )
        if reason is None:
            eligible.append(channel)
        else:
            skipped.append(DraftSkipped(channel=channel, reason=reason))

    if not eligible:
        if skipped and all(s.reason == "already drafted" for s in skipped):
            message = "Every requested channel already has a first-touch draft"
        else:
            message = (
                "No email, LinkedIn profile or opted-in WhatsApp number on file — "
                "research the prospect first"
            )
        raise CloudError(422, "prospect.no_channel", message)

    icp = await _icp_for(workspace_id, doc)
    try:
        written = await writer_fn(_to_domain(doc), icp, list(eligible), body.instructions)
    except CloudError:
        raise
    except ResearchUnavailable as exc:
        raise CloudError(502, "prospect.draft_failed", f"Drafting failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("growth: writer run failed for prospect=%s", prospect_id, exc_info=True)
        raise CloudError(502, "prospect.draft_failed", "The writer run failed") from exc

    by_channel: dict[str, Any] = {}
    for item in written:
        if item.channel in eligible and item.channel not in by_channel:
            by_channel[item.channel] = item

    drafts: list[DraftResponse] = []
    for channel in eligible:
        item = by_channel.get(channel)
        text = item.body.strip()[:10_000] if item is not None else ""
        if not text:
            skipped.append(
                DraftSkipped(
                    channel=channel, reason="the writer returned no draft for this channel"
                )
            )
            continue
        subject = item.subject.strip()[:200] if channel == "email" else None
        if channel == "email" and not subject:
            skipped.append(
                DraftSkipped(channel=channel, reason="the writer returned an email with no subject")
            )
            continue
        drafts.append(
            await _insert_draft(
                workspace_id,
                str(doc.id),
                CreateDraftRequest(
                    channel=channel, subject=subject, body=text, variant="first_touch"
                ),
            )
        )

    if not drafts:
        raise CloudError(
            502, "prospect.draft_failed", "The writer returned no usable draft for this prospect"
        )
    return DraftProspectResponse(drafts=drafts, skipped=skipped)


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


async def update_draft(
    ctx: RequestContext, draft_id: str, body: UpdateDraftRequest
) -> DraftResponse:
    """Edit a draft's copy (subject / body / demo_url) — ONLY while it is
    still ``draft``.

    The status guard is load-bearing, not tidiness. Once a draft is
    ``proposed``, a human is reading THAT copy in the Tray, and the dispatch
    worker sends the stored body — so an edit after proposal would put text on
    the wire that nobody approved. Same reasoning at ``approved`` / ``sent``.
    Anything past ``draft`` is refused with 403 ``draft.not_editable``; revise
    by rejecting the draft and writing a new one.

    The lifecycle is untouched here: this function never reads or writes
    ``status``.
    """
    body = UpdateDraftRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_draft_in_workspace(workspace_id, draft_id)

    if doc.status != "draft":
        raise Forbidden(
            "draft.not_editable",
            f"A draft in '{doc.status}' can no longer be edited — its copy is "
            "what the gate reviews and what the worker sends. Reject it and "
            "write a new draft instead",
        )
    if body.subject is not None and doc.channel != "email":
        raise ValidationError(
            "draft.subject_not_allowed",
            f"subject is only valid on the email channel (this draft is '{doc.channel}')",
        )

    for field in ("subject", "body", "demo_url"):
        value = getattr(body, field)
        if value is not None:
            setattr(doc, field, value)
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the drafts view polls.
    return _draft_to_response(_draft_to_domain(doc))


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


async def propose_send_batch(
    ctx: RequestContext, body: ProposeBatchRequest
) -> ProposeBatchResponse:
    """Propose a selection of drafts in one call (G-10a).

    Each id goes through the EXISTING ``propose_send`` — one Instinct
    proposal per draft, filed by the same code path the single-draft route
    uses. There is deliberately no batch proposal object and no shortcut into
    ``gate_transition``: a "batch" here is a UI convenience over N gated
    proposals, and a human still approves each one in the Tray. A parallel
    mechanism would be a second way to reach ``approved``, which is exactly
    what ``GATE_OWNED_TARGETS`` exists to prevent.

    Partial success, like ``bulk_ingest``: a draft that can't be proposed
    (missing, cross-tenant, already proposed, terminal) records an indexed
    error entry and the rest still go. Nothing is rolled back — the proposals
    already filed are legitimate and a human can reject them.
    """
    body = ProposeBatchRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    proposed = 0
    failed: list[ProposeBatchError] = []
    for index, draft_id in enumerate(body.draft_ids):
        try:
            await propose_send(ctx, draft_id)
        except CloudError as exc:
            # Only the domain's own failures become per-draft entries; an
            # unexpected exception still aborts the batch loudly rather than
            # being flattened into a row the caller might ignore.
            failed.append(
                ProposeBatchError(
                    index=index,
                    draft_id=draft_id,
                    code=exc.code,
                    message=exc.message,
                )
            )
            continue
        proposed += 1

    logger.info(
        "growth.propose_batch workspace=%s requested=%d proposed=%d failed=%d",
        workspace_id,
        len(body.draft_ids),
        proposed,
        len(failed),
    )
    return ProposeBatchResponse(proposed=proposed, failed=failed)


# ---------------------------------------------------------------------------
# LinkedIn manual queue (G-8)
# ---------------------------------------------------------------------------


async def _prospects_for_drafts(workspace_id: str, drafts: list[Draft]) -> dict[str, Prospect]:
    """The drafts' prospects by id, in one workspace-scoped query. A draft
    whose prospect is gone (or whose ref is malformed) has no entry, and the
    queue callers skip it rather than crash."""
    prospect_oids = []
    for draft in drafts:
        try:
            prospect_oids.append(PydanticObjectId(draft.prospect_id))
        except Exception:  # noqa: BLE001 — malformed ref == orphan, skipped by callers
            continue
    prospects: dict[str, Prospect] = {}
    if prospect_oids:
        async for pdoc in _ProspectDoc.find(
            {"workspace": workspace_id, "_id": {"$in": prospect_oids}}
        ):
            prospects[str(pdoc.id)] = _to_domain(pdoc)
    return prospects


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
    prospects = await _prospects_for_drafts(workspace_id, drafts)

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
                prospect_domain=prospect.domain,
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


def _queue_heading(item: LinkedInQueueItemResponse) -> str:
    """Title one queue section, honestly, whatever is known about the prospect.

    A prospect can be just a domain: an import of bare domains files rows with
    an empty name and company, and research fills them in later. So the heading
    joins whichever of (name, company) exist and falls back to the DOMAIN,
    which is always known — never to a placeholder word. ``Unknown — Unknown``
    reads like a corrupted row; ``northwinddental.com`` reads like a company
    nobody has researched yet, which is the truth.
    """
    known = [part for part in (item.prospect_name, item.prospect_company) if part.strip()]
    return " — ".join(known) or item.prospect_domain or "(no domain)"


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
        lines.append(f"## {_queue_heading(head)}")
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
# Per-channel delivery queues + mock delivery
# ---------------------------------------------------------------------------

_QUEUE_STATUSES = ("proposed", "approved", "sent")


def _queue_recipient(prospect: Prospect, channel: str) -> str | None:
    """Who a draft on this channel goes to — the same address the delivery
    paths resolve (email takes the first entry that looks like an address)."""
    if channel == "email":
        return next((e for e in prospect.emails if e and "@" in e), None)
    if channel == "whatsapp":
        return prospect.whatsapp_number or None
    return prospect.linkedin_url or None


async def _latest_logs_by_draft(workspace_id: str, draft_ids: list[str]) -> dict[str, Any]:
    """The newest ``MessageLog`` row per draft, in one query: rows come back
    newest first and the first seen per draft wins."""
    if not draft_ids:
        return {}
    latest: dict[str, Any] = {}
    cursor = _MessageLogDoc.find({"workspace": workspace_id, "draft_id": {"$in": draft_ids}}).sort(
        [("createdAt", -1), ("_id", -1)]
    )
    async for row in cursor:
        latest.setdefault(row.draft_id, row)
    return latest


def _delivery_state(row: Any) -> DeliveryStateResponse:
    return DeliveryStateResponse(
        outcome=row.outcome,
        provider=row.provider,
        mock=row.provider == MOCK_DELIVERY_PROVIDER,
        error=row.error or None,
        sent_at=iso_utc(row.sent_at),
        at=iso_utc(getattr(row, "updatedAt", None) or getattr(row, "createdAt", None)),
    )


async def delivery_queue(
    ctx: RequestContext, channel: str, *, limit: int = 100
) -> list[DeliveryQueueItemResponse]:
    """One channel's outbound queue: its ``proposed`` / ``approved`` / ``sent``
    drafts, newest first, each with its prospect, the resolved recipient and
    the latest delivery attempt (``None`` before the first one). Drafts whose
    prospect is gone are skipped, as in the LinkedIn queue."""
    workspace_id = _require_workspace(ctx)
    cursor = (
        _DraftDoc.find(
            {
                "workspace": workspace_id,
                "channel": channel,
                "status": {"$in": list(_QUEUE_STATUSES)},
            }
        )
        .sort(-_DraftDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    drafts = [_draft_to_domain(doc) async for doc in cursor]
    prospects = await _prospects_for_drafts(workspace_id, drafts)
    logs = await _latest_logs_by_draft(workspace_id, [d.id for d in drafts])

    items: list[DeliveryQueueItemResponse] = []
    for draft in drafts:
        prospect = prospects.get(draft.prospect_id)
        if prospect is None:
            continue
        row = logs.get(draft.id)
        items.append(
            DeliveryQueueItemResponse(
                draft=_draft_to_response(draft),
                prospect_name=prospect.name,
                prospect_company=prospect.company,
                prospect_domain=prospect.domain,
                tier=prospect.tier,
                to=_queue_recipient(prospect, channel),
                opted_in=prospect.opted_in,
                delivery=_delivery_state(row) if row is not None else None,
            )
        )
    return items


async def get_growth_settings(ctx: RequestContext) -> GrowthSettingsResponse:
    """The active workspace's growth settings. 404 when the workspace itself
    cannot be found."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    settings = await workspace_service.get_settings(_require_workspace(ctx))
    return GrowthSettingsResponse(mock_delivery=settings.growth_mock_delivery)


async def update_growth_settings(
    ctx: RequestContext, body: UpdateGrowthSettingsRequest
) -> GrowthSettingsResponse:
    """Persist the growth settings onto the active workspace's settings,
    leaving every sibling setting as it was."""
    body = UpdateGrowthSettingsRequest.model_validate(body)
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    settings = await workspace_service.patch_settings(
        ctx, _require_workspace(ctx), {"growth_mock_delivery": body.mock_delivery}
    )
    return GrowthSettingsResponse(mock_delivery=settings.growth_mock_delivery)


async def mock_delivery_enabled(workspace_id: str) -> bool:
    """System seam: is mock delivery on for this workspace? Raises when the
    workspace cannot be read — ``mock_delivery.is_mock_delivery_on`` is the
    caller that turns any failure into "off"."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    settings = await workspace_service.get_settings(workspace_id)
    return bool(settings.growth_mock_delivery)


def _still_sending(row: Any, stale_after: float) -> bool:
    """A ``sending`` row younger than ``stale_after`` seconds. An older one was
    stranded by a restart that killed its in-process task."""
    if getattr(row, "outcome", None) != "sending":
        return False
    started = getattr(row, "createdAt", None)
    if started is None:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (datetime.now(UTC) - started).total_seconds() < stale_after


async def deliver_approved(ctx: RequestContext, channel: str) -> DeliverApprovedResponse:
    """Start mock delivery for this channel's ``approved`` drafts that have no
    attempt in flight — the rescue for drafts approved before mock delivery
    was switched on, or whose in-process delivery a restart cut short.

    422 ``queue.not_deliverable`` for a channel mock delivery does not cover
    (LinkedIn is sent by hand); 409 ``growth.mock_delivery_off`` when the
    workspace has not switched it on.
    """
    workspace_id = _require_workspace(ctx)
    if channel not in MOCK_DELIVERY_CHANNELS:
        raise ValidationError(
            "queue.not_deliverable",
            f"'{channel}' drafts are not delivered by the app — send them by hand",
        )
    if not await mock_delivery_enabled(workspace_id):
        raise ConflictError(
            "growth.mock_delivery_off",
            "Mock delivery is off for this workspace — switch it on in growth settings first",
        )

    from pocketpaw_ee.cloud.growth import mock_delivery

    draft_ids = [
        str(doc.id)
        async for doc in _DraftDoc.find(
            {"workspace": workspace_id, "channel": channel, "status": "approved"}
        ).limit(500)
    ]
    logs = await _latest_logs_by_draft(workspace_id, draft_ids)
    stale_after = mock_delivery.stale_after_seconds()
    started = [
        draft_id
        for draft_id in draft_ids
        if not _still_sending(logs.get(draft_id), stale_after)
        and mock_delivery.start_mock_delivery(workspace_id, draft_id, channel)
    ]
    # no-event: the delivery task writes MessageLog rows; the queue view polls.
    return DeliverApprovedResponse(started=started)


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
    background job. An empty ``workspace_id`` returns ``None`` outright rather
    than issuing an unscoped query (guard carried over from G-6's reader).
    """
    if not workspace_id:
        return None
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


def _as_utc(raw: datetime | None) -> datetime | None:
    """Mongo returns naive datetimes; anchor them to UTC."""
    if raw is None:
        return None
    return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw


async def _sent_at_by_draft(draft_ids: list[str]) -> dict[str, datetime]:
    """Newest successful send timestamp per draft, read off the send record.

    One batched query for the whole sweep page rather than a lookup per draft.
    A draft can have several attempts (a failure and its retry each write a
    row), so the NEWEST ``sent`` row wins — that is the last time the prospect
    actually heard from us, which is what the silence window measures from.

    # global-read: paired with the cross-tenant scan in
    # ``list_sent_drafts_for_followup`` — the ids come from that scan's own
    # rows, and every value is handed straight back to it, so nothing crosses
    # a tenant boundary that the draft scan had not already crossed.
    """
    if not draft_ids:
        return {}
    newest: dict[str, datetime] = {}
    async for row in _MessageLogDoc.find(
        {"draft_id": {"$in": draft_ids}, "outcome": "sent", "sent_at": {"$ne": None}}
    ):
        stamp = _as_utc(row.sent_at)
        if stamp is None:
            continue
        current = newest.get(row.draft_id)
        if current is None or stamp > current:
            newest[row.draft_id] = stamp
    return newest


def _resolved_sent_at(doc: _DraftDoc, logged: datetime | None = None) -> datetime | None:
    """When this draft actually went out.

    Prefers ``MessageLog.sent_at`` — the timestamp the dispatch worker wrote
    when the provider accepted the message (``logged``, batched in by
    ``_sent_at_by_draft``). Falls back to the draft's ``updatedAt``, which for
    a draft sitting in ``sent`` IS the moment of the ``sent`` transition, since
    the status flip is the last write to that row.

    The fallback is not dead code. It covers drafts that predate the send
    record, and the ``linkedin`` channel, which never produces one at all: a
    LinkedIn draft is sent by hand and recorded through the mark-sent route, so
    the status flip is the only timestamp that exists for it.
    """
    return _as_utc(logged) or _as_utc(getattr(doc, "updatedAt", None))


async def list_sent_drafts_for_followup(*, limit: int = 500) -> list[dict[str, Any]]:
    """Every draft currently sitting in ``sent``, oldest-touched first.

    global-read: the follow-up cron sweeps ALL tenants under the worker's
    system identity — there is no request workspace to filter on. Each row
    carries its own ``workspace_id`` and the caller re-enters per-workspace
    reads through the scoped helpers below, so nothing crosses tenants
    downstream.

    Returns lightweight wire dicts (not domain objects) so the sweep never
    needs the doc class. ``sent_at`` is resolved per ``_resolved_sent_at``,
    with the send-record timestamps batched in by one extra query; the caller
    applies the age threshold, keeping the delay policy in one place. The
    oldest-first sort means the ``limit`` cap sheds the NEWEST sends — the ones
    furthest from being due — when a backlog exceeds it.
    """
    cursor = (
        _DraftDoc.find({"status": "sent"})
        .sort(+_DraftDoc.updatedAt)  # type: ignore[operator]
        .limit(limit)
    )
    docs = [doc async for doc in cursor]
    logged = await _sent_at_by_draft([str(doc.id) for doc in docs])
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
            "sent_at": _resolved_sent_at(doc, logged.get(str(doc.id))),
        }
        for doc in docs
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


# ---------------------------------------------------------------------------
# Discovery seams (feat/growth-discovery)
#
# The discovery run and its cron live in ``growth/discovery.py`` and cannot
# touch a doc class (import-linter "Growth" contract), so every read and write
# they need is a named function here — the same arrangement ``followups`` has.
# All of them take an explicit ``workspace_id`` because discovery runs under a
# worker/system identity, mirroring ``upsert_by_domain`` and ``gate_transition``.
# ---------------------------------------------------------------------------


async def get_icp_system(workspace_id: str, icp_id: str) -> IcpResponse:
    """Read an ICP under the worker's system identity (still tenant-scoped: a
    cross-workspace id raises NotFound exactly as the HTTP ``get_icp`` does)."""
    doc = await _fetch_icp_in_workspace(workspace_id, icp_id)
    return _icp_to_response(_icp_to_domain(doc))


async def prospect_exists_by_domain(workspace_id: str, domain: str) -> bool:
    """Is this company already in the workspace's pipeline?

    Discovery asks BEFORE filing, and skips the ones that are. Two reasons,
    and the second is the load-bearing one:

    1. Finding a company you already have is not a discovery. Filing it again
       would spend the run's ``max_per_run`` budget on nothing.
    2. Discovery only ever INSERTS, so no automated pass touches a live
       prospect at all. ``upsert_by_domain`` no longer walks ``status``
       backwards on its own (it is set-only since the review fixes), so this
       is now defence in depth rather than the sole protection — but the
       property worth keeping is the stronger one: a daily cron should not be
       able to edit a prospect a human is working, by any route.

    Normalises the domain the same way the DTO does, so a research result
    naming ``https://www.Acme.com/about`` matches the stored ``acme.com``.
    """
    normalised = _normalise_domain(domain)
    if not normalised:
        return False
    doc = await _ProspectDoc.find_one({"workspace": workspace_id, "domain": normalised})
    return doc is not None


async def count_discovered_since(workspace_id: str, since: datetime) -> int:
    """How many prospects DISCOVERY has filed for this workspace since ``since``.

    The monthly ceiling reads this before every run. It counts by ``source``
    rather than by ``icp_id`` on purpose: the bound is on how much automated
    volume one workspace generates in a period, and splitting the work across
    five ICPs must not multiply the allowance by five. Manually created and
    imported rows are not counted — a human typing a domain is not the thing
    the ceiling is protecting anyone from.
    """
    return await _ProspectDoc.find(
        {"workspace": workspace_id, "source": "discovery", "createdAt": {"$gte": since}}
    ).count()


async def list_due_icps(cadences: list[str], *, limit: int = 500) -> list[IcpResponse]:
    """Every ACTIVE ICP whose cadence is in ``cadences``, across all tenants.

    # global-read: the discovery cron runs under the worker's system identity
    # with no workspace context — it exists precisely to serve every tenant on
    # one tick. Identical to ``list_sent_drafts_for_followup``: the read is
    # global, and every row it returns carries its own ``workspace_id``, which
    # the sweep threads back into the tenant-scoped run.

    Oldest-first so a workspace that has been waiting longest is served first
    if the batch is capped. Paused ICPs and ``cadence="off"`` never appear —
    the cron cannot run something a human switched off.
    """
    docs = (
        # Sorted by last_run_at ASCENDING (missing sorts first in Mongo), so
        # the workspace that has genuinely waited longest is served first.
        # ``createdAt`` was the wrong key: it is immutable, so when the batch
        # cap bit, the SAME newest ICPs were skipped every single day rather
        # than the starvation rotating. A deployment with more weekly ICPs than
        # the cap could leave newer daily hunts permanently unrun.
        await _IcpDoc.find({"status": "active", "cadence": {"$in": cadences}})
        .sort([("last_run_at", 1), ("_id", 1)])
        .limit(limit)
        .to_list()
    )
    return [_icp_to_response(_icp_to_domain(doc)) for doc in docs]


async def mark_icp_run(workspace_id: str, icp_id: str, when: datetime) -> None:
    """Stamp ``last_run_at`` after a run. Best-effort and idempotent.

    Deliberately NOT the due-check's input: the sweep decides what is due from
    the cadence and the tick, so a worker that was down for three days resumes
    with one normal run rather than a backlog that fires all at once. This
    field answers an operator's "has this thing actually been running?", which
    is a different question from "should it run now".
    """
    doc = await _IcpDoc.find_one({"_id": PydanticObjectId(icp_id), "workspace": workspace_id})
    if doc is None:
        return
    doc.last_run_at = when
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the ICP view polls.


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


# ---------------------------------------------------------------------------
# WhatsApp dispatch + inbound (G-6)
# ---------------------------------------------------------------------------


async def record_delivery_attempt(
    workspace_id: str,
    *,
    draft_id: str,
    prospect_id: str,
    channel: str,
    provider: str,
    to_address: str,
    status: str,
    blocked_reason: str = "",
    opted_in_at_attempt: bool = False,
    error: str = "",
) -> str:
    """Write the row for one delivery attempt on any channel/provider; return
    its id. ``sending`` rows are finalised later by ``finish_delivery_attempt``;
    ``blocked`` rows are guard refusals that never reached a provider."""
    doc = _MessageLogDoc(
        workspace=workspace_id,
        draft_id=draft_id,
        prospect_id=prospect_id,
        channel=channel,
        provider=provider,
        to_address=to_address,
        outcome=status,
        blocked_reason=blocked_reason,
        opted_in_at_attempt=opted_in_at_attempt,
        error=error[:500] or None,
    )
    await doc.insert()
    # no-event: growth has no realtime subscriber in v1; the sends view polls.
    return str(doc.id)


async def record_whatsapp_attempt(
    workspace_id: str,
    *,
    draft_id: str,
    prospect_id: str,
    to_number: str,
    status: str,
    blocked_reason: str = "",
    opted_in_at_attempt: bool = False,
) -> str:
    """Write the compliance row for one MSG91 WhatsApp send attempt.

    Written BEFORE the provider is called (``status="sending"``) so an attempt
    that crashes mid-flight still leaves a trace, and so the rate-cap window
    counts in-flight attempts rather than only completed ones. Guard refusals
    write ``status="blocked"`` with the machine-readable ``blocked_reason`` and
    are never followed by a provider call.
    """
    return await record_delivery_attempt(
        workspace_id,
        draft_id=draft_id,
        prospect_id=prospect_id,
        channel="whatsapp",
        provider="msg91",
        to_address=to_number,
        status=status,
        blocked_reason=blocked_reason,
        opted_in_at_attempt=opted_in_at_attempt,
    )


async def finish_delivery_attempt(
    log_id: str,
    *,
    workspace_id: str,
    status: str,
    provider_message_id: str = "",
    error_code: str = "",
    error: str = "",
) -> None:
    """Finalise a ``sending`` row to ``sent`` / ``failed``, whatever the
    channel or provider.

    ``error`` is truncated here rather than at the call site so no caller can
    accidentally persist a full provider response (which may echo request
    headers) into the log.
    """
    try:
        oid = PydanticObjectId(log_id)
    except Exception:  # noqa: BLE001 — nothing to finalise
        return
    # Tenant-filtered, per cloud rule 7: the seam takes a bare id, so a caller
    # that ever passed a request-supplied one must not be able to finalise
    # another tenant's delivery audit row.
    doc = await _MessageLogDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        return
    doc.outcome = status
    doc.provider_message_id = provider_message_id
    doc.error_code = error_code
    doc.error = error[:500]
    if status == "sent" and doc.sent_at is None:
        # The send timestamp the follow-up sweep reads off this row. Set here
        # (not at insert) because the row starts life as ``sending``, before
        # the provider has accepted anything.
        doc.sent_at = datetime.now(UTC)
    await doc.save()  # bumps updatedAt
    # no-event: growth has no realtime subscriber in v1; the sends view polls.


# The WhatsApp dispatch branch's name for the same finaliser.
finish_whatsapp_attempt = finish_delivery_attempt


async def count_whatsapp_attempts_since(workspace_id: str, since: datetime) -> int:
    """Count attempts that REACHED the provider in the window.

    ``blocked`` rows are excluded on purpose: a refused attempt never touched
    Meta, so it must not consume the quality-rating budget the cap protects.
    ``sending`` rows ARE counted — an in-flight send is already committed.
    """
    return await _MessageLogDoc.find(
        {
            "workspace": workspace_id,
            # Scoped to the channel now that email and WhatsApp share one send
            # record — an email send must never consume the WhatsApp cap.
            "channel": "whatsapp",
            # Mock deliveries never reach Meta, so they spend none of its budget.
            "provider": {"$ne": MOCK_DELIVERY_PROVIDER},
            "outcome": {"$in": sorted(PROVIDER_REACHED_OUTCOMES)},
            "createdAt": {"$gte": since},
        }
    ).count()


def _number_variants(number: str) -> list[str]:
    """The exact stored spellings an inbound number most often matches.

    MSG91 reports the sender in E.164 digits with no ``+``; a prospect row may
    have been imported with a ``+``. Those three spellings cover the common
    case with a plain indexed equality match — anything more decorated falls
    through to ``_loose_number_regex``.
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return []
    variants = {number.strip(), digits, f"+{digits}"}
    return [v for v in variants if v]


def _loose_number_regex(number: str) -> str:
    """A separator-tolerant pattern for the same digits.

    Prospects imported from a CSV or a directory carry human formatting —
    ``+91 98765 43210``, ``+91-98765-43210``, ``(91) 98765 43210``. Normalising
    the column would mean a migration plus a write-path change shared with
    every other /growth slice, so the read path absorbs the variance instead:
    the digits in order, with any non-digits allowed between them. Only used
    when the exact-match query misses, so the common inbound event still costs
    one indexed lookup.
    """
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        return ""
    return r"^\D*" + r"\D*".join(digits) + r"\D*$"


async def _has_sent_whatsapp_draft(workspace_id: str, prospect_id: str) -> bool:
    count = await _DraftDoc.find(
        {
            "workspace": workspace_id,
            "prospect_id": prospect_id,
            "channel": "whatsapp",
            "status": "sent",
        }
    ).count()
    return count > 0


async def record_whatsapp_inbound_reply(number: str, text: str = "") -> int:
    """Apply an inbound WhatsApp reply and mark the prospect replied.

    What the reply does to consent depends on ``text`` (``whatsapp_reply_intent``):
    a STOP word clears ``opted_in`` and stamps ``whatsapp_opt_out_at``; a START
    word sets ``opted_in`` and clears the stamp; any other reply sets
    ``opted_in`` unless the prospect has opted out, which only START undoes.

    Returns how many prospect rows were updated — 0 for a number we don't hold,
    which the webhook turns into a plain 200 so the endpoint never reveals
    whether a number exists in the system.

    # global-read: the MSG91 inbound webhook is unauthenticated by nature (the
    # provider is the caller) and its payload carries no workspace, so the
    # lookup starts from the number alone. Tenancy is re-narrowed immediately:
    # when any workspace has actually WhatsApp'd this number, only those
    # workspaces' rows are touched — a tenant that merely holds the same
    # prospect never learns that someone else's outreach got a reply. Scoping
    # the resolve by the receiving ``integrated_number`` instead (exact, per
    # WABA) is the follow-up once the connector row is queryable by config.
    """
    variants = _number_variants(number)
    if not variants:
        return 0

    docs = await _ProspectDoc.find({"whatsapp_number": {"$in": variants}}).to_list()
    if not docs:
        # Second pass for human-formatted stored numbers. See _loose_number_regex.
        pattern = _loose_number_regex(number)
        if pattern:
            docs = await _ProspectDoc.find({"whatsapp_number": {"$regex": pattern}}).to_list()
    if not docs:
        return 0

    # A plain inbound reply is treated as the opt-in signal (a STOP word is an
    # opt-out instead), whether or not we had a sent draft outstanding, so
    # "nobody messaged them" must still record it. What it may NOT do is record
    # consent, or an opt-out, for a tenant the reply cannot be attributed to.
    #
    # The old `messaged or docs` fallback did exactly that: when no workspace
    # had messaged the number, EVERY tenant holding it was stamped
    # opted_in=True. Two agencies buying the same directory export was enough,
    # and opted_in is the flag whatsapp.py checks before a business-initiated
    # send — so one person's message manufactured consent for an agency that
    # had never contacted them.
    #
    # Attribution, in order: whoever actually messaged them; failing that, the
    # sole workspace holding the number. If several tenants hold it and none
    # has messaged, the reply is genuinely unattributable and nothing is
    # written. Scoping by the receiving ``integrated_number`` removes the
    # ambiguity properly and stays the follow-up.
    messaged = [d for d in docs if await _has_sent_whatsapp_draft(d.workspace, str(d.id))]
    if messaged:
        targets = messaged
    elif len({d.workspace for d in docs}) == 1:
        targets = docs
    else:
        logger.info(
            "growth.whatsapp_inbound: %d row(s) across %d workspaces hold this "
            "number and none had messaged it — unattributable, recording nothing",
            len(docs),
            len({d.workspace for d in docs}),
        )
        return 0

    intent = whatsapp_reply_intent(text)
    now = datetime.now(UTC)
    for doc in targets:
        if intent == "stop":
            doc.opted_in = False
            doc.whatsapp_opt_out_at = now
        elif intent == "start":
            doc.opted_in = True
            doc.whatsapp_opt_out_at = None
        elif doc.whatsapp_opt_out_at is None:
            doc.opted_in = True
        doc.status = "replied"
        await doc.save()  # bumps updatedAt
        # no-event: growth has no realtime subscriber in v1.

        sent_drafts = await _DraftDoc.find(
            {
                "workspace": doc.workspace,
                "prospect_id": str(doc.id),
                "channel": "whatsapp",
                "status": "sent",
            }
        ).to_list()
        for draft in sent_drafts:
            # sent→replied is legal per DRAFT_TRANSITIONS and is not a
            # gate-owned target; the gate seam is used because this caller runs
            # under a system identity with no RequestContext.
            await gate_transition(doc.workspace, str(draft.id), "replied")

    logger.info(
        "growth.whatsapp_inbound: reply matched %d prospect row(s) across %d candidate(s)",
        len(targets),
        len(docs),
    )
    return len(targets)


__all__ = [
    "bulk_ingest",
    "count_whatsapp_attempts_since",
    "create",
    "create_draft",
    "create_followup_draft",
    "deliver_approved",
    "delivery_queue",
    "draft_prospect",
    "finish_delivery_attempt",
    "finish_whatsapp_attempt",
    "gate_transition",
    "get",
    "get_draft_for_dispatch",
    "get_growth_settings",
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
    "mock_delivery_enabled",
    "propose_send",
    "record_delivery_attempt",
    "record_message_log",
    "record_whatsapp_attempt",
    "record_whatsapp_inbound_reply",
    "research_prospect",
    "transition",
    "update",
    "update_growth_settings",
    "upsert_by_domain",
]
