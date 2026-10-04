# ee/pocketpaw_ee/cloud/partners/service_admin.py — public, cross-tenant partner reads
# and the public application.
#
# Nothing here has a caller workspace: the directory and ``/partners/{slug}``
# are read before sign-in, and ``apply`` is filed by someone who is not a tenant
# yet. That is why these live in ``service_admin`` and not ``service``; every
# function carries ``# admin-cross-tenant: <reason>``.
#
# Invariants a reader must not break:
#   * Every read filters ``partner.status == "active" AND partner.public is True``
#     (``_PUBLIC``). An applied, suspended or opted-out partner is NotFound.
#   * The wire is ``_public`` -> ``PartnerPublicOut`` (an allow-list). The
#     ``footer_name``, ``billing_country``, ``founding`` and ``status`` stop here.
#   * A partner's sites are its public Discover listings, read through
#     ``discover.service_admin.list_public_for_workspaces`` (the newest
#     ``SITES_PER_PARTNER`` per partner, one capped query each); this module
#     builds no listing card of its own.
#   * ``apply`` stores exactly ONE ``PartnerApplication`` (the platform's review
#     queue, not a tenant's data) after the global daily cap and Turnstile pass,
#     and emits ``PartnerApplied``. Nothing is written to any workspace. The
#     operator routes in ``cloud/platform/partners.py`` list and review the queue
#     through ``list_applications`` / ``review_application``; this module is the
#     only importer of the ``PartnerApplication`` doc.

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, time
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import NotFound, RateLimited, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import PartnerApplied
from pocketpaw_ee.cloud._core.turnstile import verify_turnstile
from pocketpaw_ee.cloud.discover import service_admin as discover_admin
from pocketpaw_ee.cloud.discover.dto import PublicListingResponse
from pocketpaw_ee.cloud.models.partner_application import PartnerApplication
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.partners.dto import (
    PartnerApplicationOut,
    PartnerApplicationPage,
    PartnerApplyIn,
    PartnerDirectoryPage,
    PartnerPublicOut,
)

# Applications accepted per UTC day, all addresses together, so a distributed
# flood (many IPs under the 5/hour each) cannot fill the operator queue. 500 is
# far above any real intake. ponytail: counted before the insert, so concurrent
# submissions can overshoot by the ones in flight; a reservation row fixes that.
APPLY_DAILY_CAP = 500
# Public listings shown per partner (directory card and profile), newest first.
SITES_PER_PARTNER = 12

_PUBLIC: dict[str, Any] = {"deleted_at": None, "partner.status": "active", "partner.public": True}


def _public(ws: _WorkspaceDoc, sites: list[dict]) -> PartnerPublicOut:
    p = ws.partner
    assert p is not None  # _PUBLIC matched
    return PartnerPublicOut(
        slug=p.slug or "",
        display_name=p.display_name or "",
        city=p.city,
        country=p.country,
        services=list(p.services),
        bio=p.bio,
        contact_url=p.contact_url,
        tier=p.tier,
        joined_at=p.joined_at,
        sites=[PublicListingResponse.model_validate(card) for card in sites],
    )


async def _with_sites(rows: list[_WorkspaceDoc]) -> list[PartnerPublicOut]:
    ids = [str(r.id) for r in rows]
    sites = await discover_admin.list_public_for_workspaces(ids, per_workspace=SITES_PER_PARTNER)
    return [_public(r, sites.get(str(r.id), [])) for r in rows]


async def list_directory(
    *,
    city: str | None = None,
    service: str | None = None,
    cursor: str | None = None,
    limit: int = 24,
) -> PartnerDirectoryPage:
    """A page of public active partners, newest first. ``city`` is a
    case-insensitive exact match; ``service`` one of the partner's services."""
    # admin-cross-tenant: the public directory spans every workspace by design.
    query = dict(_PUBLIC)
    if city:
        query["partner.city"] = {"$regex": f"^{re.escape(city)}$", "$options": "i"}
    if service:
        query["partner.services"] = service
    if cursor:
        try:
            query["_id"] = {"$lt": PydanticObjectId(cursor)}
        except (InvalidId, TypeError, ValueError):
            raise ValidationError("partners.bad_cursor", "Invalid cursor") from None
    # ponytail: ``partner.city`` regex and ``partner.public`` are unindexed; the
    # partner count is small. Add a (partner.public, partner.status, _id) index
    # when the directory outgrows a collection scan.
    rows = await _WorkspaceDoc.find(query).sort([("_id", -1)]).limit(limit + 1).to_list()
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return PartnerDirectoryPage(items=await _with_sites(rows[:limit]), next_cursor=next_cursor)


async def get_public(slug: str) -> PartnerPublicOut:
    """One public active partner by slug, else NotFound."""
    # admin-cross-tenant: a public profile is readable by anyone.
    ws = await _WorkspaceDoc.find_one({**_PUBLIC, "partner.slug": slug})
    if ws is None:
        raise NotFound("partner", slug)
    return (await _with_sites([ws]))[0]


def _ip_hash(remote_ip: str | None) -> str | None:
    # A dedupe / abuse key, not anonymisation: the IPv4 space is enumerable.
    return hashlib.sha256(remote_ip.encode()).hexdigest() if remote_ip else None


async def _applications_today() -> int:
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    return await PartnerApplication.find({"createdAt": {"$gte": start}}).count()


async def apply(payload: Any, *, remote_ip: str | None = None) -> str:
    """Store a partner application for operators to review; returns its id.

    Order: body validation, the global daily cap (429 ``partners.apply_daily_limit``),
    Turnstile (400 ``partners.turnstile_failed``), then one insert and
    ``PartnerApplied`` (id and country only, never the contact details).
    """
    # admin-cross-tenant: the applicant has no workspace; the row is the
    # platform's review queue, not a tenant's.
    body = PartnerApplyIn.model_validate(payload)
    if await _applications_today() >= APPLY_DAILY_CAP:
        raise RateLimited(
            "partners.apply_daily_limit",
            "We're not taking more applications today - please try again tomorrow.",
        )
    await verify_turnstile(body.turnstile_token, remote_ip, code="partners.turnstile_failed")
    doc = PartnerApplication(
        name=body.name,
        email=str(body.email),
        city=body.city,
        country=body.country,
        services=list(body.services),
        message=body.message,
        source_ip_hash=_ip_hash(remote_ip),
    )
    await doc.insert()
    await emit(PartnerApplied(data={"application_id": str(doc.id), "country": body.country}))
    return str(doc.id)


def _application_out(doc: PartnerApplication) -> PartnerApplicationOut:
    return PartnerApplicationOut(
        id=str(doc.id),
        name=doc.name,
        email=doc.email,
        city=doc.city,
        country=doc.country,
        services=list(doc.services),
        message=doc.message,
        status=doc.status,
        note=doc.note,
        reviewed_by=doc.reviewed_by,
        reviewed_at=doc.reviewed_at,
        created_at=doc.createdAt,
    )


async def list_applications(
    *, status: str | None = None, cursor: str | None = None, limit: int = 50
) -> PartnerApplicationPage:
    """Operator page of applications, newest first; ``status`` filters, the
    cursor is the last row's id (operator-only, so a raw id is fine here)."""
    # admin-cross-tenant: the platform's own queue; the caller is an operator.
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if cursor:
        try:
            query["_id"] = {"$lt": PydanticObjectId(cursor)}
        except (InvalidId, TypeError, ValueError):
            raise ValidationError("partners.bad_cursor", "Invalid cursor") from None
    rows = await PartnerApplication.find(query).sort([("_id", -1)]).limit(limit + 1).to_list()
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return PartnerApplicationPage(
        items=[_application_out(r) for r in rows[:limit]], next_cursor=next_cursor
    )


async def get_application(application_id: str) -> PartnerApplicationOut:
    """One application by id, else NotFound."""
    # admin-cross-tenant: operator read of the platform's queue.
    return _application_out(await _application_doc(application_id))


async def _application_doc(application_id: str) -> PartnerApplication:
    try:
        oid = PydanticObjectId(application_id)
    except (InvalidId, TypeError, ValueError):
        raise NotFound("partner_application", application_id) from None
    doc = await PartnerApplication.get(oid)
    if doc is None:
        raise NotFound("partner_application", application_id)
    return doc


async def review_application(
    application_id: str, *, status: str, note: str, reviewed_by: str
) -> PartnerApplicationOut:
    """Set an application's status and note; stamps who reviewed it and when."""
    # admin-cross-tenant: operator write on the platform's queue.
    # no-event: the platform audit row at the route is the record.
    doc = await _application_doc(application_id)
    doc.status = status  # type: ignore[assignment]  # the DTO narrowed it
    doc.note = note
    doc.reviewed_by = reviewed_by
    doc.reviewed_at = datetime.now(UTC)
    await doc.save()
    return _application_out(doc)


__all__ = [
    "APPLY_DAILY_CAP",
    "SITES_PER_PARTNER",
    "apply",
    "get_application",
    "get_public",
    "list_applications",
    "list_directory",
    "review_application",
]
