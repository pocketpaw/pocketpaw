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
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
#
# Updated 2026-05-30 (follow-up item 4): added GET /sites/{site_id}/domains — an
# authed, tenant-scoped read of the site's domains + statuses (same
# require_plan_feature("fabric") router gate + require_action_any_workspace
# scoping as the other authed sites reads) so the Domains tab can rehydrate on
# reload. A site in another workspace surfaces as a 404 (via the service _load).

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import (
    DomainRequest,
    DomainStatusResponse,
    PublishRequest,
    SiteResponse,
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
    # Read the pocket's rippleSpec + theme via the pockets service (the source
    # of truth). ``get`` returns the resolved wire dict and raises NotFound /
    # Forbidden itself (it never returns None), so no extra existence check is
    # needed here — the standard error envelope maps those to 404 / 403.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(body.pocket_id, ctx.user_id)
    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}

    doc = await sites_service.publish(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=body.pocket_id,
        ripple_spec=ripple_spec,
        theme=theme,
        name=pocket.get("name", ""),
    )
    return sites_service._to_response(doc)


@router.get("/sites", response_model=list[SiteResponse])
async def list_sites(ctx: RequestContext = Depends(request_context)) -> list[SiteResponse]:
    return await sites_service.list_for_workspace(ctx.workspace_id)


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
