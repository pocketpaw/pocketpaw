# ee/pocketpaw_ee/sites/router.py — REST surface for the Sites control plane.
# Publish acts on a pocket the caller can edit; domain ops are workspace-scoped
# and gated by the same plan feature (fabric) + action (fabric.write/read) as
# the Leads surface (Task 3.4). Mirrors the leads router's context/deps wiring.
#
# Pocket read: uses pockets_service.get(pocket_id, user_id) — the real
# single-pocket reader, which returns a wire DICT (camelCase keys: rippleSpec,
# name) and RAISES NotFound when missing / access-denied (it does not return
# None). theme is pulled from the rippleSpec subtree.
#
# Updated 2026-06-01 (Phase 4 — chat→create-site): publish_site now delegates to
# sites_service.publish_pocket(), the shared pocket-read + publish path the new
# in-process MCP tool also calls. The pocket-read/theme-derive logic that used to
# be inline here lives in the service now so the chat and REST surfaces share one
# code path.
#
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
#
# Updated 2026-05-30 (follow-up item 4): added GET /sites/{site_id}/domains — an
# authed, tenant-scoped read of the site's domains + statuses (same
# require_plan_feature("fabric") router gate + require_action_any_workspace
# scoping as the other authed sites reads) so the Domains tab can rehydrate on
# reload. A site in another workspace surfaces as a 404 (via the service _load).
#
# Updated 2026-06-06 (feat/1345-draft-published): added two pocket-keyed reads for
# the draft/published state machine (pocketpaw#1345), both gated fabric.read:
#   * GET /sites/by-pocket/{pocket_id}/status  — the Draft/Live badge state
#     (status + is_live + draft/published version pointers). Keyed by SOURCE
#     pocket id because a draft has no site_id until first publish.
#   * GET /sites/by-pocket/{pocket_id}/preview — the current DRAFT content the
#     builder iframe renders (rippleSpec or svelte source map), fixing the
#     dead-published-URL preview.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import (
    DomainRequest,
    DomainStatusResponse,
    PreviewResponse,
    PublishRequest,
    SiteResponse,
    SiteStatusResponse,
)

router = APIRouter(
    tags=["Sites"],
    dependencies=[Depends(require_plan_feature("fabric"))],
)


@router.post("/sites/publish", response_model=SiteResponse)
async def publish_site(
    body: PublishRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Compile the pocket's rippleSpec, smoke-gate, deploy, and persist."""
    # The pocket-read + theme-derive + publish is shared with the in-process MCP
    # tool via ``publish_pocket``. ``pockets_service.get`` (called inside) raises
    # NotFound / Forbidden itself, which the standard error envelope maps to
    # 404 / 403 — no extra existence check is needed here.
    doc = await sites_service.publish_pocket(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=body.pocket_id,
    )
    return sites_service._to_response(doc)


@router.get("/sites", response_model=list[SiteResponse])
async def list_sites(ctx: RequestContext = Depends(request_context)) -> list[SiteResponse]:
    return await sites_service.list_for_workspace(ctx.workspace_id)


@router.get(
    "/sites/by-pocket/{pocket_id}/status",
    response_model=SiteStatusResponse,
)
async def site_status(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteStatusResponse:
    """The draft/published + is_live badge state for a pocket's site. Works
    before the first publish (no Site doc yet) — the version state comes from the
    versions log and ``is_live`` is False until a deploy succeeds."""
    return await sites_service.site_status(workspace_id=ctx.workspace_id, pocket_id=pocket_id)


@router.get(
    "/sites/by-pocket/{pocket_id}/preview",
    response_model=PreviewResponse,
)
async def site_preview(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> PreviewResponse:
    """The current DRAFT content the builder preview renders (rippleSpec for a
    ripple site, svelte source map for a svelte site) — NOT the published URL.
    Fixes the builder preview iframing a dead local-serve address."""
    return await sites_service.preview(workspace_id=ctx.workspace_id, pocket_id=pocket_id)


@router.post("/sites/{site_id}/domains", response_model=DomainStatusResponse)
async def add_domain(
    site_id: str,
    body: DomainRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> DomainStatusResponse:
    return await sites_service.add_domain(
        workspace_id=ctx.workspace_id, site_id=site_id, hostname=body.hostname
    )


@router.get("/sites/{site_id}/domains", response_model=list[DomainStatusResponse])
async def list_domains(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> list[DomainStatusResponse]:
    """Tenant-scoped read of the site's domains + statuses (Domains-tab
    rehydration). A site in another workspace surfaces as a 404."""
    return await sites_service.list_domains(workspace_id=ctx.workspace_id, site_id=site_id)


@router.get("/sites/{site_id}/domains/{hostname}/status", response_model=DomainStatusResponse)
async def domain_status(
    site_id: str,
    hostname: str,
    ctx: RequestContext = Depends(request_context),
) -> DomainStatusResponse:
    return await sites_service.domain_status(
        workspace_id=ctx.workspace_id, site_id=site_id, hostname=hostname
    )
