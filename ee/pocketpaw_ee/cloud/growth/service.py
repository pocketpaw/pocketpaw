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

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, NotFound
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.domain import Prospect
from pocketpaw_ee.cloud.growth.dto import (
    CreateProspectRequest,
    ProspectResponse,
    UpdateProspectRequest,
)
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


__all__ = [
    "create",
    "get",
    "list_prospects",
    "update",
    "upsert_by_domain",
]
