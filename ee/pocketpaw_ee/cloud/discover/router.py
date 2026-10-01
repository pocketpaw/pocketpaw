# router.py — FastAPI router for the Discover index (/discover).
#
# Created 2026-10-01 (feat/discover-index, DS-1). Thin HTTP surface over
# ``discover.service_admin`` (public reads) and ``discover.service`` (signed-in
# use / report). Mounted under /api/v1 from ``ee/pocketpaw_ee/cloud/__init__.py``.
#
# Routes:
#   GET  /discover                     — PUBLIC page of listings (per-IP 60/min)
#   GET  /discover/{listing_id}        — PUBLIC one listing (per-IP 60/min)
#   POST /discover/{listing_id}/use    — signed in: copy the item into your workspace
#   POST /discover/{listing_id}/report — signed in: report a listing (204)
#
# The public reads take no user dependency: the dashboard auth middleware lets
# /api/v1/* through and the EE auth bridge only stamps a user when a token is
# present, so no exemption entry is needed (same as GET /meetings/by-code/{code}).
# No Beanie doc import here (import-linter "Discover" contracts); errors are
# CloudError subclasses mapped by the global handler.

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud._core.rate_limit import rate_limit_discover_public
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
    "/{listing_id}",
    response_model=PublicListingResponse,
    dependencies=[Depends(rate_limit_discover_public)],
)
async def get_listing(listing_id: str) -> dict:
    """PUBLIC — no sign-in. One unhidden listing; 404 when missing or hidden."""
    return await service_admin.get_public(listing_id)


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


@router.post("/{listing_id}/report", status_code=204)
async def report_listing(
    listing_id: str,
    body: ReportListingRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    """Report a listing. One report per user; a repeat is a no-op. 403
    ``discover.own_listing`` for your own listing."""
    await service.report_listing(workspace_id, user_id, listing_id, body)
    return Response(status_code=204)
