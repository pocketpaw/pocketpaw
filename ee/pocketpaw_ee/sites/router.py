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
# Updated 2026-06-18 (feat/sites-devserver, Phase 2 / P2a): added POST
# /sites/by-pocket/{pocket_id}/dev-preview — start (or reuse) a live Vite dev-server
# for the pocket's EDITING preview and return its localhost url. Delegates to
# sites_service.dev_preview_pocket → the DevServerManager singleton (long-lived
# `vite dev` per pocket, HMR in ~ms vs a per-edit rebuild). Carries fabric.write (it
# spawns a process / mutates server state); a missing pocket is a 404 (the pockets
# service raises NotFound during materialize). Publish/editable are unchanged.
# Updated 2026-06-19 (P2b-backend — revert endpoint): added POST
# /sites/by-pocket/{pocket_id}/versions/{version_no}/revert — revert a pocket's site
# to a prior version by ordinal. Delegates to sites_service.revert_pocket_version,
# which resolves version_no → the durable ArtifactVersion row (tenant-scoped, main
# branch) and writes a NEW forward-moving draft snapshot of that version's content;
# the normal review/publish flow then applies (request-publish → merge gate). Returns
# the new draft as a SiteVersionResponse (the same row shape the versions timeline
# uses). fabric.write (it creates a draft); an unknown version_no is a 404 (the
# service raises ValueError).
# Updated 2026-06-20 (DS-3 — read a dynamic site's D1 data): added two fabric.read
# operator data-view endpoints over a DYNAMIC site's per-tenant Cloudflare D1.
#   * GET /sites/by-pocket/{pocket_id}/data — list the site's tables (from the
#     pocket spec's ``objects``). Always lists the schema; ``available`` is False
#     with reason="live_on_cloudflare_only" in local/dev mode (no live D1).
#   * GET /sites/by-pocket/{pocket_id}/data/{table} — read one table's rows
#     (bounded LIMIT). ``table`` is validated against the spec's declared objects
#     (unknown → 404, never interpolated); values bind through query params. Local
#     mode degrades cleanly (available=False, columns still listed, no rows).
#   Both delegate to sites_service; a NON-dynamic pocket → 422 (not_dynamic), a
#   missing / access-denied pocket → 404 / 403 (the pockets service raises it).
#   Tenant-scoped on ctx — the data view is read-only (no request body).

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import (
    AuditResponse,
    DevPreviewResponse,
    DomainRequest,
    DomainStatusResponse,
    MakeEditableRequest,
    PublishRequest,
    RequestPublishResponse,
    SiteDataRowsResponse,
    SiteDataTablesResponse,
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
        site_plan_key=body.site_plan_key,
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


@router.post("/sites/by-pocket/{pocket_id}/dev-preview", response_model=DevPreviewResponse)
async def dev_preview_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> DevPreviewResponse:
    """Start (or reuse) a live Vite dev-server for the pocket's EDITING preview and
    return its localhost URL (Phase 2 / P2a): {pocket_id, url}.

    The editor frames this URL so edits hot-reload over Vite HMR in ~ms instead of
    rebuilding the whole site per edit. A running server for the pocket is reused
    (touched); otherwise one is materialized from the pocket's current source
    (PERF-3 persistent dir, cached node_modules) and started on an ephemeral port.
    Publish / make_site_editable are unchanged — this is the editing preview only.

    Carries fabric.write (it spawns a process / mutates server state), matching the
    other by-pocket write actions. A missing / access-denied pocket surfaces as a
    404 (the pockets service raises NotFound itself during materialize)."""
    return await sites_service.dev_preview_pocket(
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


@router.get("/sites/by-pocket/{pocket_id}/data", response_model=SiteDataTablesResponse)
async def site_data_tables_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteDataTablesResponse:
    """List a DYNAMIC site's data tables for the operator data-view (DS-3):
    {pocket_id, available, reason, tables}. The table list comes from the pocket
    spec's ``objects`` (the declared D1 tables), so it is populated even when the
    live D1 is not reachable. ``available`` is False with
    ``reason="live_on_cloudflare_only"`` in local/dev mode (no live D1) so the UI
    degrades cleanly — it can show the schema but explain why no rows load.

    A NON-dynamic pocket (a static landing / brochure) has no data store, so the
    service raises ValidationError("sites.not_dynamic") → 422. A missing /
    access-denied pocket surfaces as 404 / 403 (the pockets service raises it).
    Tenant-scoped on ctx."""
    return await sites_service.list_site_data_tables(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id
    )


@router.get("/sites/by-pocket/{pocket_id}/data/{table}", response_model=SiteDataRowsResponse)
async def site_data_rows_by_pocket(
    pocket_id: str,
    table: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteDataRowsResponse:
    """Read the rows of ONE table of a DYNAMIC site's D1 (DS-3): {pocket_id,
    table, available, reason, columns, rows}. ``rows`` is the live D1 rows (capped
    by a LIMIT); ``columns`` is the table's declared field names.

    SQL safety: ``table`` is validated against the pocket spec's declared
    ``objects`` — an unknown table is a 404 (NotFound("site_table")), never
    interpolated into SQL; every value binds through query params. In local/dev
    mode (no live D1) ``available`` is False with
    ``reason="live_on_cloudflare_only"`` and ``rows`` empty, but ``columns`` is
    still listed from the spec. A NON-dynamic pocket → 422
    ("sites.not_dynamic"); a missing / access-denied pocket → 404 / 403.
    Tenant-scoped on ctx."""
    return await sites_service.read_site_data_table(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id, table=table
    )


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


@router.post(
    "/sites/by-pocket/{pocket_id}/versions/{version_no}/revert",
    response_model=SiteVersionResponse,
)
async def revert_version_by_pocket(
    pocket_id: str,
    version_no: int,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteVersionResponse:
    """Revert a pocket's site to a prior version by ordinal (P2b-backend).

    Revert is FORWARD-MOVING: it writes a NEW draft on the main branch whose
    content snapshots the target version, then the normal review/publish flow
    applies — the operator request-publishes the new draft and the merge gate
    takes the reverted content live. History is never rewritten; the revert is its
    own auditable lineage step. Tenant-scoped on ctx.workspace_id; a version_no the
    pocket does not have (or one under another workspace) raises ValueError → 404.
    Carries fabric.write — it creates a draft. Returns the new draft row so the UI
    can show the freshly-created version (and feed it to request-publish)."""
    try:
        draft = await sites_service.revert_pocket_version(
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            pocket_id=pocket_id,
            version_no=version_no,
        )
    except ValueError as exc:
        # Unknown / cross-tenant version_no → 404 (nothing to revert to).
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return SiteVersionResponse(
        id=str(draft.id),
        version_no=draft.version_no,
        branch=draft.branch,
        status=draft.status,
        label=draft.label,
        author=draft.author,
        created_at=draft.created_at.isoformat(),
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
