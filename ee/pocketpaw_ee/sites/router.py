# ee/pocketpaw_ee/sites/router.py — REST surface for the Sites control plane.
# Publish acts on a pocket the caller can edit; domain ops are workspace-scoped
# and gated by the same plan feature (fabric) + action (fabric.write/read) as
# the Leads surface (Task 3.4). Mirrors the leads router's context/deps wiring.
#
# Updated 2026-08-24 (SP-2 — draft preview joins the ephemeral build lane): GET
# ``/sites/by-pocket/{pocket_id}/native-artifact`` now has TWO response shapes and
# ``build_status`` says which. A cache hit is unchanged (the render, ``build_status``
# ``"none"``); a cold miss queues the armed build in a Daytona sandbox and answers
# immediately with empty ``body_html`` / ``css`` plus ``build_status`` / ``build_job_id``
# to poll. The endpoint stops 5xxing as ``sites.generator_failed``, which is what it did
# on every cold preview in the deployed container — there is no ``bun`` there to build
# with. An enqueue that fails is a 503, deliberately: a job id for a job nobody will run
# makes a client poll forever.
#
# Updated 2026-08-12 (the custom-domain routing lane): added DELETE
# ``/sites/{site_id}/domains/{hostname}``. Domains could be connected and never
# disconnected, so a removed or re-pointed domain left its Cloudflare custom hostname
# on the zone permanently — consuming quota, pointing at a Worker that might no longer
# exist, and (because Cloudflare rejects duplicate hostnames) blocking that domain from
# ever being connected to a different site. Gated on ``fabric.write`` like ``add_domain``:
# it releases a name on the shared zone that anyone may then claim.
#
# Updated 2026-08-12 (sites Settings consolidation): three endpoints for the
# owner's client record — GET + PATCH ``/sites/{site_id}/client`` and POST
# ``/sites/{site_id}/invoices``. They exist because the builder's Settings surface
# had shipped a Client panel and a "Record payment" button backed by nothing:
# component state with a comment saying persistence was a later task, so every
# value typed there was lost on reload. Gated by the same fabric.read /
# fabric.write actions as the domain ops above, tenant-scoped through the
# service's ``_load``. NONE OF THIS CHARGES ANYONE — recording a receipt is the
# owner writing down that their client paid, and is unrelated to the owner's own
# subscription with us (``/sites/publish``'s ``site_plan_key``).
#
# Pocket read: uses pockets_service.get(pocket_id, user_id) — the real
# single-pocket reader, which returns a wire DICT (camelCase keys: rippleSpec,
# name) and RAISES NotFound when missing / access-denied (it does not return
# None). theme is pulled from the rippleSpec subtree.
#
# Updated 2026-08-07 (SC-3 — the card stops lying after a republish): new
# POST /sites/{site_id}/preview-refresh, the manual half of the preview policy
# (automatic capture on every successful deploy + an explicit refresh). It is the
# one capture in this subsystem that REPORTS failure rather than swallowing it —
# every other one hangs off a publish, which it may never endanger; this one hangs
# off a button press, whose whole value is the answer.
#
# Updated 2026-07-17 (fix/sites-prewarm-origin): ``publish_site`` and
# ``apply_leaf_edits_by_pocket`` now thread the request ``Origin`` header into the
# service as ``prewarm_origin`` so the background native-artifact pre-warm builds with
# the SAME origin the browser's ``GET /native-artifact`` view resolves — otherwise the
# pre-warm falls back to PAW_SITES_BUILDER_ORIGIN, its content hash never matches the
# view's, and every view is a cold miss. The PUBLIC deploy is unchanged (still plain —
# ``prewarm_origin`` steers only the pre-warmed armed artifact), mirroring the existing
# Origin precedence /editable + /dev-preview + /native-artifact already use.
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
# require_plan_feature("sites") router gate + require_action_any_workspace
# scoping as the other authed sites reads) so the Domains tab can rehydrate on
# reload. A site in another workspace surfaces as a 404 (via the service _load).
#
# Updated 2026-06-25 (decouple-sites-from-fabric): the router plan gate moved from
# require_plan_feature("fabric") to require_plan_feature("sites") — Sites now
# unlocks on the consumer "sites" flag (go+), decoupled from the enterprise-only
# Fabric ontology, which keeps the "fabric" flag.
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
# Updated 2026-06-26 (feat/sites-dev-bridge-source, S1 — dev source carries the
# edit-bridge): dev_preview_by_pocket now resolves a ``builder_origin`` (the
# request ``Origin`` header, with the PAW_SITES_BUILDER_ORIGIN env fallback applied
# in the service) and threads it to dev_preview_pocket → ensure_dev_server →
# _default_materialize → GeneratorClient.build(builder_origin=..., static_build=
# False). This makes the dev-server-materialized SOURCE carry SE-1's section anchors
# + gated edit-bridge (mirroring /editable's origin precedence), so the hover-edit
# overlay works against the dev server — without it the dev path materialized
# anchorless source and flipping BRIDGE_IN_DEV regressed the overlay. static_build
# stays False (no prod build on the dev path — only the generate/scaffold step needs
# builder_origin for the source injection).
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
#
# Updated 2026-07-01 (NE-4b — native-editing leaf-edit persist): added POST
# /sites/by-pocket/{pocket_id}/leaf-edits — the native editor forwards its
# already-rendered {uid, op} leaf edits and this persists them as a reviewable
# Branch draft (splice via the apply-leaf-edit CLI → set_svelte_source_file), NO
# rebuild. fabric.write; tenant-scoped; returns one verdict per edit. A non-svelte
# pocket or empty batch → 422; a missing / access-denied pocket → 404 / 403 (the
# pockets service raises it). Delegates to sites_service.apply_leaf_edits.
#
# Updated 2026-07-01 (NE-5b — native-artifact endpoint): added GET
# /sites/by-pocket/{pocket_id}/native-artifact — serves the armed svelte build's
# <body> inner HTML + concatenated CSS as {pocket_id, body_html, css} so the native
# editor shadow-renders the site instead of framing an iframe. fabric.write (it
# ensures/triggers the armed build); the builder_origin is resolved from the request
# Origin header (the service applies the PAW_SITES_BUILDER_ORIGIN env fallback),
# mirroring /editable + /dev-preview. A pocket with no native edit lane → 422; a
# missing / access-denied pocket → 404 / 403 (the pockets service raises it).
# Delegates to sites_service.get_native_artifact.
#
# Updated 2026-08-22 (RX-2): the 422 guard is no longer svelte-only — it is now
# ``has_native_edit_lane`` (svelte + react), and the response body's error code
# changed from ``pocket.not_svelte_site`` to ``pocket.no_native_edit_lane``. html
# and ripple are still rejected: html's served artifact IS its source (selected
# through its own srcdoc, no build to render) and ripple has no source map. The
# rest of the wire contract (request/response, origin resolution, 404/403) is
# unchanged.
#
# Updated 2026-07-17 (feat/sites-native-artifact-no-build): get_native_artifact is now a
# READ-THROUGH cache — a repeat view with unchanged source is a disk read with ZERO
# subprocess builds (publish + the post-edit pre-warm populate the store ahead of the
# view). fabric.write is RETAINED because a COLD miss still builds (mutates on-disk
# state), so the endpoint can still trigger work; it is not a pure read. The wire
# contract (request/response, origin resolution, 422/404/403) is unchanged.
#
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): added the two IMPORT
# endpoints, both tenant-scoped writes (fabric.write) under the router's sites plan
# gate like every sibling mutation:
#   * POST /sites/import — multipart zip upload (25MB cap enforced while reading the
#     upload). Delegates to import_service.import_zip_site: safe in-memory unpack
#     (zip-slip + decompression-bomb guards), mint pocket + DRAFT Site doc, publish
#     through the existing html/static deploy path (binary files ride the
#     generator's ``assets`` base64 sideband — cross-repo seam), persist an
#     ``import_report`` on the Site doc, Journal event. Returns the SiteResponse
#     (now carrying import_report).
#   * POST /sites/import/from-url — {url} → 202 {site_id, pocket_id,
#     status:"queued"}. Validates the URL shape and mints the draft Site with a
#     queued import_report.
#
# Updated 2026-07-23 (SI-5 — feat/sites-import-crawler): the from-url crawler is
# REAL. The endpoint contract is unchanged (202 queued), but validation now also
# runs the crawler's SSRF shape floors (non-http(s) scheme, embedded credentials,
# non-80/443 port, literal private/loopback/metadata IP → 422 before any write),
# and the service schedules the same-site crawl as a background task: crawl →
# the zip import pipeline → import_report flips to "imported"/"failed" with
# crawl stats. See import_service.crawl_site_from_url + url_crawler.

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.cloud.auth.service import resolve_display_names
from pocketpaw_ee.sites import import_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import (
    AuditResponse,
    DevPreviewResponse,
    DomainRequest,
    DomainStatusResponse,
    ImportFromUrlRequest,
    ImportFromUrlResponse,
    LeafEditsRequest,
    LeafEditsResponse,
    LeafEditVerdict,
    MakeEditableRequest,
    NativeArtifactResponse,
    PublishRequest,
    RequestPublishResponse,
    SiteClientResponse,
    SiteClientUpdate,
    SiteDataRowsResponse,
    SiteDataTablesResponse,
    SiteEntitlementsResponse,
    SiteInvoiceCreate,
    SitePreviewRefreshResponse,
    SitePreviewResponse,
    SiteResponse,
    SiteStatusResponse,
    SiteVersionResponse,
    VersionHistoryResponse,
)
from pocketpaw_ee.versions import service as versions_service

router = APIRouter(
    tags=["Sites"],
    dependencies=[Depends(require_plan_feature("sites"))],
)


@router.post("/sites/publish", response_model=SiteResponse)
async def publish_site(
    body: PublishRequest,
    request: Request,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Compile the pocket's rippleSpec, smoke-gate, deploy, and persist."""
    # The pocket-read + theme-derive + publish is shared with the in-process MCP
    # tool via ``publish_pocket``. ``pockets_service.get`` (called inside) raises
    # NotFound / Forbidden itself, which the standard error envelope maps to
    # 404 / 403 — no extra existence check is needed here.
    #
    # ORIGIN-STABILITY (fix/sites-prewarm-origin): thread the request Origin header as
    # ``prewarm_origin`` so the background native-artifact pre-warm builds with the
    # SAME origin the browser's GET /native-artifact view resolves (its own request
    # Origin) — otherwise the pre-warm falls back to PAW_SITES_BUILDER_ORIGIN, its hash
    # never matches the view's, and every view is a cold miss. This does NOT arm the
    # PUBLIC deploy (builder_origin stays unset here — the public site stays plain); it
    # only steers the pre-warmed armed artifact the native editor consumes.
    doc = await sites_service.publish_pocket(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=body.pocket_id,
        site_plan_key=body.site_plan_key,
        prewarm_origin=request.headers.get("origin") or None,
        # Also the checkout's return base for a PAID publish. The frontend sends the
        # whole page to ``checkout_url``, so with no return_url the buyer pays and
        # has no route back into the app. Falls back to the
        # ``dodo_checkout_return_base`` config inside the service when absent.
        origin=request.headers.get("origin") or None,
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


@router.post("/sites/by-pocket/{pocket_id}/leaf-edits", response_model=LeafEditsResponse)
async def apply_leaf_edits_by_pocket(
    pocket_id: str,
    body: LeafEditsRequest,
    request: Request,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> LeafEditsResponse:
    """Persist native-editor leaf edits as a reviewable Branch draft (NE-4b).

    Splices the forwarded ``{uid, op}`` edits into the pocket's svelte source via the
    paw-sites apply-leaf-edit CLI and writes a draft — NO rebuild (the native editor
    already renders the change optimistically; skipping the per-edit iframe rebuild
    is the UX win over the old edit path). Returns one verdict per edit. A missing /
    access-denied pocket is a 404 (the pockets service raises NotFound); a non-svelte
    pocket or an empty edit batch is a 422.

    ORIGIN-STABILITY (fix/sites-prewarm-origin): thread the request Origin header as
    ``prewarm_origin`` so the background native-artifact pre-warm this schedules builds
    with the SAME origin the browser's GET /native-artifact view resolves (its own
    request Origin) — the native editor calls both from the same dashboard, so without
    this the pre-warm falls back to PAW_SITES_BUILDER_ORIGIN and its hash never matches
    the view's (mirrors the /sites/publish fix)."""
    results = await sites_service.apply_leaf_edits(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        edits=[e.model_dump() for e in body.edits],
        prewarm_origin=request.headers.get("origin") or None,
    )
    return LeafEditsResponse(
        pocket_id=pocket_id,
        results=[LeafEditVerdict(**r) for r in results],
    )


@router.get(
    "/sites/by-pocket/{pocket_id}/native-artifact",
    response_model=NativeArtifactResponse,
)
async def native_artifact_by_pocket(
    pocket_id: str,
    request: Request,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> NativeArtifactResponse:
    """Serve the armed svelte build's {pocket_id, body_html, css} for native shadow
    render (NE-5b).

    ``body_html`` is the built page's ``<body>`` inner HTML (the data-uid-stamped
    leaves + the embedded ``paw-edit-manifest`` script); ``css`` is the built
    stylesheet(s) concatenated. The native editor injects both into a shadow root
    instead of framing an iframe.

    READ-THROUGH cache (feat/sites-native-artifact-no-build): the service serves a
    prior render from disk when the pocket's render inputs are unchanged (ZERO builds
    — a plain VIEW never triggers a build).

    A COLD MISS ANSWERS WITH A BUILD TO POLL, NOT WITH A RENDER (SP-2). The armed build
    now runs in an ephemeral Daytona sandbox rather than in this container — there is no
    ``bun`` here, which is why a cold preview used to 5xx as ``sites.generator_failed``.
    The response then carries empty ``body_html`` / ``css`` plus ``build_status``
    (``queued`` / ``building`` / ``failed``) and ``build_job_id``; the client re-fetches
    until ``build_status`` reads ``"none"``, which is the served-render shape. An enqueue
    that fails is a 503, never a job id — a client handed one for a job nobody will run
    would poll forever.

    Carries fabric.write because a cold miss still queues the armed build (spends a
    sandbox) — it is not a pure read. The builder origin — which the armed build
    needs to stamp data-uid + the manifest — is resolved from the request's ``Origin``
    header, with the service applying the ``PAW_SITES_BUILDER_ORIGIN`` env fallback when
    it is absent (the same precedence as ``/editable`` / ``/dev-preview``), so the call
    works with no header. A pocket with no native edit lane is a 422 — svelte and
    react are armable, html (served straight from its source) and ripple are not;
    a missing / access-denied pocket surfaces as a 404 / 403 (the pockets service
    raises it inside the service)."""
    # Mirror /editable + /dev-preview origin resolution: the request Origin header
    # here; the service applies the PAW_SITES_BUILDER_ORIGIN env fallback when blank.
    builder_origin = request.headers.get("origin") or ""
    result = await sites_service.get_native_artifact(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        builder_origin=builder_origin,
    )
    return NativeArtifactResponse(**result)


@router.post("/sites/import", response_model=SiteResponse)
async def import_site(
    file: UploadFile = File(...),
    name: str = Form(""),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Import an uploaded site zip (SI-4): unpack safely in memory (zip-slip +
    decompression-bomb guards in the service), mint the html pocket + DRAFT Site
    doc, publish through the existing html/static deploy path (binary files ride
    the generator's ``assets`` base64 sideband), persist the ``import_report`` on
    the Site doc, and return the SiteResponse (carrying the report).

    The 25MB upload cap gates PROCESSING, not ingress: Starlette's multipart
    parser spools the whole request body to a temp file before this handler
    runs, so raw-ingress bounding belongs to the fronting proxy's body limit.
    The handler reads at most cap+1 bytes of the part, and the cap is re-checked
    in the service for
    direct callers. Oversized → 413; a malformed/hostile archive → 422 (the
    service's ValidationError codes map through the standard error envelope).
    Tenant-scoped on ctx (fabric.write, sites plan gate at the router level)."""
    cap = import_service.MAX_IMPORT_ZIP_BYTES
    data = await file.read(cap + 1)
    if len(data) > cap:
        raise HTTPException(413, f"Import zip exceeds the {cap // (1024 * 1024)}MB upload cap")
    doc = await import_service.import_zip_site(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        data=data,
        name=name,
    )
    return sites_service._to_response(doc, pattern=import_service.IMPORT_PATTERN, engine="html")


@router.post(
    "/sites/import/from-url",
    response_model=ImportFromUrlResponse,
    status_code=202,
)
async def import_site_from_url(
    body: ImportFromUrlRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ImportFromUrlResponse:
    """Queue a from-url import (SI-4 contract, SI-5 crawler): validate the URL
    (shape + SSRF floors — bad scheme/port/credentials or a literal non-public IP
    → 422 before anything is minted), mint the pocket + DRAFT Site doc with a
    queued ``import_report``, schedule the background same-site crawl, and return
    202 {site_id, pocket_id, status:"queued"} immediately. The crawl runs the zip
    import pipeline and flips the report to "imported"/"failed" with crawl stats.
    Tenant-scoped on ctx (fabric.write), like every sibling sites mutation."""
    queued = await import_service.import_from_url(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, url=body.url
    )
    return ImportFromUrlResponse(**queued)


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
    request: Request,
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

    The materialized dev source carries the gated edit-bridge so the hover-edit
    overlay works against the dev server. The builder origin is resolved the SAME
    way as ``/editable``: the request's ``Origin`` header (the dashboard the call
    came from), with the service falling back to the configured
    ``PAW_SITES_BUILDER_ORIGIN`` when the header is absent, so the dev-served source
    is anchored + bridged exactly like the static editable build.

    Carries fabric.write (it spawns a process / mutates server state), matching the
    other by-pocket write actions. A missing / access-denied pocket surfaces as a
    404 (the pockets service raises NotFound itself during materialize)."""
    # Mirror /editable's origin resolution: the request Origin header here; the
    # service applies the PAW_SITES_BUILDER_ORIGIN env fallback when it is blank.
    builder_origin = request.headers.get("origin") or ""
    return await sites_service.dev_preview_pocket(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=pocket_id,
        builder_origin=builder_origin,
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
    ctx.workspace_id. An unversioned pocket reads an empty list (not a 404).

    Statuses go over the wire through ``resolve_legacy_statuses``: rows written
    before 2026-08-21 say ``"reverted"`` whether an edit replaced them or the
    owner discarded them, and this endpoint feeds the owner-facing timeline,
    where that word reads as a rollback that never happened. The resolver splits
    them by lineage. Rows written since carry their own status and pass through
    untouched."""
    rows = await sites_service.version_history(workspace_id=ctx.workspace_id, pocket_id=pocket_id)
    shown = versions_service.resolve_legacy_statuses(rows)
    # ``author`` on the row is ``str(user.id)``, so the timeline was captioning
    # every version with a 24-character ObjectId — technically who did it, and
    # unreadable. One batched lookup for the whole timeline (never per row), and
    # ``.get(id, id)`` keeps the raw value for an author the resolver cannot
    # name, exactly as Mission Control does it.
    names = await resolve_display_names({r.author for r in rows if r.author})
    return VersionHistoryResponse(
        pocket_id=pocket_id,
        versions=[
            SiteVersionResponse(
                id=str(r.id),
                version_no=r.version_no,
                branch=r.branch,
                status=shown[str(r.id)],
                label=r.label,
                author=names.get(r.author, r.author) if r.author else None,
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


@router.post("/sites/{site_id}/preview-refresh", response_model=SitePreviewRefreshResponse)
async def refresh_site_preview(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SitePreviewRefreshResponse:
    """Re-photograph the site's gallery card image on demand (SC-3).

    Capture is automatic on every successful deploy, which handles the case that
    matters — the design changed — so this exists for the ones a deploy cannot fix:
    a capture that failed at the time (Cloudflare unconfigured, quota, a render that
    timed out), or a draft whose markup only became buildable later. Without it, the
    only way to correct a card was to republish an unchanged site.

    A POST because it spends a paid remote render and rewrites the Site, and
    ``fabric.write`` for the same reason. Named ``preview-refresh`` rather than
    ``preview`` deliberately: on this router ``by-pocket/{id}/preview`` already means
    the draft CONTENT the builder renders, and two different meanings of "preview"
    one path segment apart is how a client ends up calling the wrong one.

    SYNCHRONOUS, and it can fail. Every other capture in this subsystem is
    fire-and-forget behind a swallow, because a picture may never cost anyone a
    publish. This one was asked for by a person who is watching a spinner, so it
    waits for the render (seconds) and surfaces a real error instead of a 200
    carrying the same stale url they pressed the button to replace. A site in
    another workspace is a 404; nothing renderable yet is a 422.
    """
    return await sites_service.refresh_site_preview(workspace_id=ctx.workspace_id, site_id=site_id)


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


@router.delete("/sites/{site_id}/domains/{hostname}", status_code=204)
async def remove_domain(
    site_id: str,
    hostname: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> None:
    """Disconnect a custom domain — delete its Worker route and its Cloudflare
    custom hostname, then drop it from the site.

    Gated on ``fabric.write`` like ``add_domain``: this releases a hostname on the
    shared zone, and once released anyone may claim it. 204 because there is nothing
    left to describe; a hostname this site does not have is a 404."""
    await sites_service.remove_domain(
        workspace_id=ctx.workspace_id, site_id=site_id, hostname=hostname
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


@router.get("/sites/{site_id}/entitlements", response_model=SiteEntitlementsResponse)
async def get_site_entitlements(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteEntitlementsResponse:
    """What this site may do, so the UI can disable a control and say why.

    Its own endpoint rather than fields on the list response: the domain-slot
    answer needs a workspace-wide count of sites already holding a domain, and
    riding that on ``GET /sites`` would run one count per card. The gallery stays a
    single query; only a surface that actually offers a gated control pays for the
    lookup.
    """
    return await sites_service.site_entitlements(workspace_id=ctx.workspace_id, site_id=site_id)


@router.get("/sites/{site_id}/client", response_model=SiteClientResponse)
async def get_site_client(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteClientResponse:
    """The owner's record of who this site is for, plus the receipts they have
    logged against that client. A site with nothing recorded returns a blank
    record; only a missing or cross-tenant site is a 404."""
    return await sites_service.get_site_client(workspace_id=ctx.workspace_id, site_id=site_id)


@router.patch("/sites/{site_id}/client", response_model=SiteClientResponse)
async def update_site_client(
    site_id: str,
    body: SiteClientUpdate,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteClientResponse:
    """Patch the client record. Omitting a field leaves it untouched; sending it
    empty clears it. Returns the whole updated record."""
    return await sites_service.update_site_client(
        workspace_id=ctx.workspace_id, site_id=site_id, body=body
    )


@router.post("/sites/{site_id}/invoices", response_model=SiteClientResponse)
async def record_site_invoice(
    site_id: str,
    body: SiteInvoiceCreate,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteClientResponse:
    """Log one manual receipt against the site's client. This records that the
    owner was paid — it does NOT charge anyone, and it is unrelated to the owner's
    own subscription with us. Returns the whole updated client record so the caller
    re-renders from one response instead of splicing the new row in locally."""
    return await sites_service.record_site_invoice(
        workspace_id=ctx.workspace_id, site_id=site_id, body=body
    )
