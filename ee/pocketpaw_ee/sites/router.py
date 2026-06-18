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
# Updated 2026-06-17 (pocketpaw#1345 backend half — by-pocket preview + status):
# added GET /sites/by-pocket/{pocket_id}/preview and
# GET /sites/by-pocket/{pocket_id}/status — the two by-pocket reads the #432
# frontend already calls (getSitePreviewByPocket / getSiteStatusByPocket). The
# backend half of #1345 never landed on dev, so every Preview-tab fetch 404'd and
# the builder showed "Nothing to preview yet". Both are authed fabric.read reads
# under the router-level fabric plan gate, scoped on ctx.workspace_id /
# ctx.user_id, matching the other authed sites reads. preview delegates to
# sites_service.preview_pocket (pockets_service.get raises NotFound → 404 itself);
# status delegates to sites_service.pocket_status (tenant-scoped Site lookup, no
# 404 — an unpublished pocket simply reads draft / not live).
# Updated 2026-06-17 (feat/sites-local-reserve): added POST /sites/reserve — the
# explicit "re-serve local sites" action. Locally-deployed sites die after a
# backend restart (the per-process static server binds an ephemeral port and is
# only started during publish), so this (re)starts the server and rewrites the
# workspace's site urls to the fresh live base via sites_service
# .reserve_local_sites(ctx.workspace_id), then returns the reconciled list. Gated
# like the other authed sites writes (fabric.write). No-op outside local mode.
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): added POST
# /sites/by-pocket/{pocket_id}/editable — republish the pocket's site as
# editable (the generated page carries SE-1's gated edit-bridge). Same fabric
# plan gate + fabric.write action scope as publish. The builder origin is
# resolved in precedence: explicit body ``builder_origin`` > request ``Origin``
# header > configured PAW_SITES_BUILDER_ORIGIN (env fallback in the service), so
# the SE-3 editor's call carries the right origin with no extra wiring.
# Updated 2026-06-18 (feat/branch-primitive-revert-history, BP-4): added two
# Branch-primitive surfaces over the BP-1 versions spine.
#   * GET /sites/by-pocket/{pocket_id}/versions — the ordered version timeline
#     for the pocket (the source pocket is the versionable artifact; scope_type=
#     "pocket"), tenant-scoped (fabric.read). Reads the durable ArtifactVersion
#     rows (list_versions) — the exact, current log — not a journal replay.
#   * POST /sites/by-pocket/{pocket_id}/request-publish — the CLEAN entry to the
#     Instinct merge gate (BP-3): the server builds the ``_artifact_change``
#     review proposal so the CLIENT never hand-builds the Instinct propose (BP-5
#     needs this). Returns the created Action (id/status) so the client shows
#     "submitted for review". fabric.write — it creates a gate item that, on
#     approve, publishes + deploys. A 400 when there is no draft to publish.
# Updated 2026-06-18 (feat/branch-primitive-audit, BP-7 — producer 2): added
# POST /sites/by-pocket/{pocket_id}/audit — run the deterministic site audit over
# the pocket's content and return findings, each with a ``fix_prompt`` the UI
# feeds to the EXISTING edit path so a fix lands as a reviewable draft (no new
# apply endpoint). It's a read of the pocket's content, so it carries fabric.read
# (the same gate as preview/status). Tenant-scoped on ctx; the pockets service
# raises NotFound → 404 itself.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import (
    AuditResponse,
    DomainRequest,
    DomainStatusResponse,
    MakeEditableRequest,
    PublishRequest,
    RequestPublishResponse,
    SitePreviewResponse,
    SiteResponse,
    SiteStatusResponse,
    SiteVersionResponse,
    VersionHistoryResponse,
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


@router.post("/sites/reserve", response_model=list[SiteResponse])
async def reserve_sites(
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> list[SiteResponse]:
    """Re-serve this workspace's locally-deployed sites and return the refreshed
    list. Locally-deployed sites stop responding after a backend restart (the
    static server binds an ephemeral port and is only started at publish time);
    this (re)starts the server and rewrites each site's url to the live base, so
    previously-deployed sites become openable again. A no-op outside local mode
    (the real Cloudflare path owns its own URLs), in which case the list comes
    back unchanged."""
    await sites_service.reserve_local_sites(ctx.workspace_id)
    return await sites_service.list_for_workspace(ctx.workspace_id)


@router.post("/sites/by-pocket/{pocket_id}/editable", response_model=SiteResponse)
async def make_site_editable(
    pocket_id: str,
    request: Request,
    body: MakeEditableRequest = MakeEditableRequest(),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Republish the pocket's site as EDITABLE (SE-2b): the regenerated page
    carries the gated edit-bridge keyed on a builder origin (the dashboard the
    page postMessages its section rects to).

    The builder origin is resolved in precedence: an explicit ``builder_origin``
    in the body (an override the SE-3 editor can pass) wins; otherwise the
    request's ``Origin`` header (the dashboard origin the call came from); and
    the service falls back to the configured ``PAW_SITES_BUILDER_ORIGIN`` when
    neither is present, so the call works with no body and no Origin header.

    The pocket read inside ``publish_pocket`` raises NotFound / Forbidden itself,
    mapped to 404 / 403 by the error envelope."""
    # Body override beats the Origin header; the service applies the env fallback
    # when both are blank. headers.get returns None when absent.
    builder_origin = (body.builder_origin or "").strip() or (request.headers.get("origin") or "")
    doc = await sites_service.make_site_editable(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        builder_origin=builder_origin,
    )
    return sites_service._to_response(doc)


@router.get("/sites", response_model=list[SiteResponse])
async def list_sites(ctx: RequestContext = Depends(request_context)) -> list[SiteResponse]:
    return await sites_service.list_for_workspace(ctx.workspace_id)


@router.get("/sites/by-pocket/{pocket_id}/preview", response_model=SitePreviewResponse)
async def preview_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SitePreviewResponse:
    """Draft content for the in-app builder Preview tab: {pocket_id, engine,
    content}. ``content`` is the pocket's rippleSpec for a ripple pocket, or the
    {path: contents} source map for a svelte pocket. A missing / access-denied
    pocket surfaces as a 404 (the pockets service raises NotFound itself)."""
    return await sites_service.preview_pocket(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id
    )


@router.get("/sites/by-pocket/{pocket_id}/status", response_model=SiteStatusResponse)
async def status_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteStatusResponse:
    """Authoritative draft/published + is_live state for a pocket: {pocket_id,
    status, is_live}. Derived from the tenant-scoped Site deployment doc — an
    unpublished pocket (no Site) reads draft / not live (NOT a 404)."""
    return await sites_service.pocket_status(workspace_id=ctx.workspace_id, pocket_id=pocket_id)


@router.post("/sites/by-pocket/{pocket_id}/audit", response_model=AuditResponse)
async def audit_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> AuditResponse:
    """Audit a pocket's published-site source and return findings (BP-7, the first
    non-editor producer): a11y (missing alt, unnamed button/link, h1 structure,
    unlabeled inputs), broken/placeholder links, and SEO head tags (title, meta
    description, Open Graph). Each finding carries a ``fix_prompt`` the UI sends to
    the EXISTING edit path (edit_svelte_component / refine) so the fix lands as a
    reviewable draft in the Tray — there is NO separate apply endpoint.

    A POST because it is an explicit, on-demand pass over the source (potentially
    a model-backed judgment tier later), not a cheap idempotent read; it still
    carries fabric.read since it only READS the pocket. A missing / access-denied
    pocket surfaces as a 404 (the pockets service raises NotFound itself). Reads
    the same draft-or-current content preview_pocket serves, so the audit matches
    what publish would build. A clean site returns an empty ``findings`` list."""
    return await sites_service.audit_pocket(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id
    )


@router.get("/sites/by-pocket/{pocket_id}/versions", response_model=VersionHistoryResponse)
async def versions_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> VersionHistoryResponse:
    """The ordered version timeline for a pocket (BP-4): every ArtifactVersion of
    the source pocket (scope_type="pocket"), oldest → newest, tenant-scoped on
    ctx.workspace_id. An unversioned pocket reads an empty list (not a 404)."""
    rows = await sites_service.version_history(workspace_id=ctx.workspace_id, pocket_id=pocket_id)
    return VersionHistoryResponse(
        pocket_id=pocket_id,
        versions=[
            SiteVersionResponse(
                id=str(r.id),
                version_no=r.version_no,
                branch=r.branch,
                status=r.status,
                label=r.label,
                author=r.author,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
    )


@router.post("/sites/by-pocket/{pocket_id}/request-publish", response_model=RequestPublishResponse)
async def request_publish_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> RequestPublishResponse:
    """Submit the pocket's current draft for review (BP-4 Part C) — the clean
    entry to the Instinct merge gate.

    The SERVER builds the ``_artifact_change`` review proposal (the client must
    NOT hand-build the Instinct propose) and returns the created Action so the
    client can show "submitted for review". Approving that Action in The Tray
    dispatches BP-3's merge executor (publish the reviewed version + deploy).

    The blob's ``workspace`` is stamped with ctx.workspace_id (never empty —
    BP-3's guard hard-403s an empty workspace claim). When the pocket has no
    current draft to publish, the service raises ValueError → 400 (nothing to
    review)."""
    try:
        action = await sites_service.request_publish_pocket(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id
        )
    except ValueError as exc:
        # No draft to publish → 400 (nothing to submit for review).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    blob = (action.parameters or {}).get("_artifact_change", {})
    return RequestPublishResponse(
        action_id=str(action.id),
        status=action.status.value if hasattr(action.status, "value") else str(action.status),
        pocket_id=pocket_id,
        to_version_id=str(blob.get("to_version_id") or ""),
        from_version_id=blob.get("from_version_id"),
    )


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
