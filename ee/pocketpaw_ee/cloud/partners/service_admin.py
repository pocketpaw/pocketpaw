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
#     ``discover.service_admin.list_public_for_workspaces`` (one query per page);
#     this module builds no listing card of its own.
#   * ``apply`` files exactly ONE Instinct proposal in the platform's own store
#     (``PLATFORM_SCOPE``, not a tenant), after Turnstile passes, and emits
#     ``PartnerApplied``. Nothing is written to any workspace.

from __future__ import annotations

import re
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import PartnerApplied
from pocketpaw_ee.cloud._core.turnstile import verify_turnstile
from pocketpaw_ee.cloud.discover import service_admin as discover_admin
from pocketpaw_ee.cloud.discover.dto import PublicListingResponse
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.partners.domain import PARTNER_APPLICATION_PARAM_KEY, PLATFORM_SCOPE
from pocketpaw_ee.cloud.partners.dto import (
    PartnerApplyIn,
    PartnerDirectoryPage,
    PartnerPublicOut,
)

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
    sites = await discover_admin.list_public_for_workspaces(ids)
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


async def apply(payload: Any, *, remote_ip: str | None = None) -> str:
    """File a partner application as one Instinct proposal for the platform.

    Turnstile first (400 ``partners.turnstile_failed``), then one ``propose`` in
    the platform scope; returns the proposal id (the route does not expose it).
    """
    # admin-cross-tenant: the applicant has no workspace; the proposal is the
    # platform's, not a tenant's.
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    body = PartnerApplyIn.model_validate(payload)
    await verify_turnstile(body.turnstile_token, remote_ip, code="partners.turnstile_failed")
    services = ", ".join(body.services)
    summary = f"{body.name} ({body.city}, {body.country}) wants to join Paw Partners: {services}."
    store = get_instinct_store(workspace_id=PLATFORM_SCOPE)
    action = await store.propose(
        pocket_id=PLATFORM_SCOPE,
        title=f"Partner application: {body.name}",
        description=summary,
        recommendation=(
            f"Review and, if accepted, set the applicant's workspace as a partner "
            f"(PUT /platform/workspaces/{{id}}/partner).\n\nContact: {body.email}"
            + (f"\n\n{body.message}" if body.message else "")
        ),
        trigger=ActionTrigger(
            type="user", source="partners.apply", reason="public partner application"
        ),
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.MEDIUM,
        parameters={
            PARTNER_APPLICATION_PARAM_KEY: {
                "kind": "partner_application",
                "schema": 1,
                **body.model_dump(exclude={"turnstile_token"}),
            }
        },
        workspace_id=PLATFORM_SCOPE,
    )
    await emit(PartnerApplied(data={"proposal_id": action.id, "country": body.country}))
    return action.id


__all__ = ["apply", "get_public", "list_directory"]
