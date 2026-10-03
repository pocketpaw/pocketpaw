# router.py — FastAPI router for the Discover index (/discover).
#
# Thin HTTP surface over ``discover.service_admin`` (public reads) and
# ``discover.service`` (signed-in use / report). Mounted under /api/v1 from
# ``ee/pocketpaw_ee/cloud/__init__.py``.
#
# Routes:
#   GET  /discover                     — PUBLIC page of listings (per-IP 60/min)
#   GET  /discover/{id_or_slug}        — PUBLIC one listing by id or slug (per-IP 60/min)
#   POST /discover/{listing_id}/use    — signed in: copy the item into your workspace
#   POST /discover/{listing_id}/report — signed in: report a listing (204; 10/hour per user)
#
# The public reads take no user dependency: the dashboard auth middleware lets
# /api/v1/* through and the EE auth bridge only stamps a user when a token is
# present, so no exemption entry is needed (same as GET /meetings/by-code/{code}).
# No Beanie doc import here (import-linter "Discover" contracts); errors are
# CloudError subclasses mapped by the global handler.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud._core.rate_limit import (
    rate_limit_discover_public,
    rate_limit_discover_report,
)
from pocketpaw_ee.cloud.discover import service, service_admin
from pocketpaw_ee.cloud.discover.dto import (
    ListPublicListingsRequest,
    PublicListingPage,
    PublicListingResponse,
    ReportListingRequest,
    UseListingRequest,
    UseListingResponse,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/discover",
    tags=["discover"],
    dependencies=[Depends(require_license)],
)


@router.get(
    "",
    response_model=PublicListingPage,
    dependencies=[Depends(rate_limit_discover_public)],
)
async def list_listings(query: ListPublicListingsRequest = Depends()) -> dict:
    """PUBLIC — no sign-in. A page of unhidden listings, newest first. 422 on a
    bad cursor; 429 ``discover.rate_limited`` past 60 reads a minute per IP."""
    return await service_admin.list_public(query)


@router.get(
    "/{id_or_slug}",
    response_model=PublicListingResponse,
    dependencies=[Depends(rate_limit_discover_public)],
)
async def get_listing(
    id_or_slug: str, source: str | None = Query(default=None, max_length=64)
) -> dict:
    """PUBLIC — no sign-in. One unhidden listing, by id or by slug; ``source``
    picks the source when two share a slug (else the oldest listing wins). 404
    when missing or hidden."""
    return await service_admin.get_public(id_or_slug, source)


@router.post("/{listing_id}/use", response_model=UseListingResponse)
async def use_listing(
    listing_id: str,
    body: UseListingRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Make your own copy of the listing's item in your workspace, through its
    source (``{pocket_id}`` for a site template)."""
    name = body.name if body else None
    return await service.use_listing(workspace_id, user_id, listing_id, name)


@router.post(
    "/{listing_id}/report",
    status_code=204,
    dependencies=[Depends(rate_limit_discover_report)],
)
async def report_listing(
    listing_id: str,
    body: ReportListingRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    """Report a listing. One report per user; a repeat is a no-op. 403
    ``discover.own_listing`` for your own listing; 429
    ``discover.report_rate_limited`` past 10 reports an hour."""
    await service.report_listing(workspace_id, user_id, listing_id, body)
    return Response(status_code=204)
