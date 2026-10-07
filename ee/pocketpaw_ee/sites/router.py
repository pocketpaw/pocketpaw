# ee/pocketpaw_ee/sites/router.py — REST surface for the Sites control plane.
#
# Thin FastAPI handlers over ``sites.service``: publish and preview, the native
# editing and asset routes, custom domains, slugs and renames, owner settings
# (metadata, branding, AI visibility, client record and invoices), site-plan
# requests, analytics and entitlements, transfers, export, delete, origin claims
# and the foreign-origin concierge.
#
# Per-site writes are gated on ``fabric.write`` (reads on ``fabric.read``) in the
# caller's workspace and are tenant-scoped, so a missing or cross-tenant site is a
# 404. Publish additionally requires edit rights on the pocket. Handlers never
# raise HTTPException and never import Beanie documents; errors are CloudErrors
# mapped by ``_core.http``.

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response, StreamingResponse

from pocketpaw.money import MONEY_UNITS_HEADER, client_sends_minor_units
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.cloud._core.rate_limit import rate_limit_slug_check
from pocketpaw_ee.cloud.auth.service import resolve_display_names
from pocketpaw_ee.sites import import_service, ownership
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import (
    AuditResponse,
    DevPreviewResponse,
    DomainRequest,
    DomainStatusResponse,
    ForeignConciergeBindRequest,
    ForeignConciergeOrigin,
    ForeignConciergeRebindRequest,
    ForeignConciergeResponse,
    HtmlArmedSourceResponse,
    ImportBriefStatusResponse,
    ImportFromUrlRequest,
    ImportFromUrlResponse,
    LeafEditsRequest,
    LeafEditsResponse,
    LeafEditVerdict,
    MakeEditableRequest,
    NativeArtifactResponse,
    OriginClaimRequest,
    OriginClaimResponse,
    OriginVerificationResponse,
    PublishRequest,
    RequestPublishResponse,
    SiteAiVisibilityUpdate,
    SiteAnalyticsResponse,
    SiteAssetDeleteRequest,
    SiteAssetListResponse,
    SiteAssetResponse,
    SiteBrandingUpdate,
    SiteBuildLogResponse,
    SiteBuildResponse,
    SiteClientResponse,
    SiteClientUpdate,
    SiteDataRowsResponse,
    SiteDataTablesResponse,
    SiteDeleteQueuedResponse,
    SiteDeleteStatusResponse,
    SiteEntitlementsResponse,
    SiteExportResponse,
    SiteInvoiceCreate,
    SiteMetadataUpdate,
    SitePlanRequestBody,
    SitePlanRequestResponse,
    SitePreviewRefreshResponse,
    SitePreviewResponse,
    SiteResponse,
    SiteStatusResponse,
    SiteTransferListResponse,
    SiteTransferOfferRequest,
    SiteTransferResponse,
    SiteVersionResponse,
    SlugAvailability,
    SlugRenameRequest,
    VersionHistoryResponse,
)
from pocketpaw_ee.versions import service as versions_service

router = APIRouter(
    tags=["Sites"],
    dependencies=[Depends(require_plan_feature("sites"))],
)


async def _may_buy_site_plan(user: object, workspace_id: str) -> bool:
    """May this caller commit the workspace to a recurring site charge?

    Asked as a QUESTION rather than enforced as a dependency, because the answer
    only matters when the request actually names a paid tier — and a free publish
    by an ordinary member is the common case that must not 403. The service makes
    the decision; this only reports the role.

    Non-raising by design: ``check_workspace_action`` raises on deny and audits
    it, which is right for a gate and wrong for a predicate. The denial that
    reaches the user comes from the service, with copy that tells them what to do
    about it, rather than a bare role error on a publish they were allowed to make.
    """
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.guards.deps import check_workspace_action

    try:
        await check_workspace_action(user, workspace_id, "sites.buy_plan")
    except CloudError:
        return False
    except Exception:  # noqa: BLE001 — a broken role read must not sell a plan
        import logging

        logging.getLogger(__name__).warning(
            "sites.publish: could not resolve the caller's role for sites.buy_plan "
            "— treating as NOT authorized to buy",
            exc_info=True,
        )
        return False
    return True


@router.post("/sites/publish", response_model=SiteResponse)
async def publish_site(
    body: PublishRequest,
    request: Request,
    ctx: RequestContext = Depends(request_context),
    # The USER, not a throwaway: publishing stays a MEMBER action, but whether
    # this caller may BUY a paid tier is a second, higher question and needs the
    # principal to ask it of.
    user: object = Depends(require_action_any_workspace("fabric.write")),
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
        purchase_authorized=await _may_buy_site_plan(user, ctx.workspace_id),
        prewarm_origin=request.headers.get("origin") or None,
    )
    return sites_service._to_response(doc)


@router.post("/sites/plan-requests", response_model=SitePlanRequestResponse)
async def request_site_plan(
    body: SitePlanRequestBody,
    ctx: RequestContext = Depends(request_context),
    user: object = Depends(require_action_any_workspace("fabric.write")),
) -> SitePlanRequestResponse:
    """Ask a workspace admin to put this site on a paid plan.

    The other side of ``sites.plan_purchase_forbidden``. A member who publishes
    and picks a paid tier is refused, correctly — a paid site is an add-on line
    on the workspace's own subscription, so choosing one spends company money.
    Before this endpoint the refusal was also a dead end: the employee who built
    the site had no route forward except finding an admin out-of-band and
    describing what they wanted.

    This files an Instinct Action instead. An admin approves it in The Tray and
    the executor performs the publish that was refused — re-checking the
    APPROVER's ``sites.buy_plan`` at that moment, so approving is what authorizes
    the spend and nothing here does.

    Gated on ``fabric.write`` (MEMBER), the same action publishing needs: asking
    is not spending, and a gate above MEMBER would refuse exactly the people this
    exists for. An ADMIN may also call it — a request they can approve themselves
    is a slower path to the same place, not a wrong one, and refusing it would
    make the client's job harder for no benefit.

    Returns the pending Action so the caller can link to it. NOTHING is published
    and NOTHING is charged on this path.
    """
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.site_plan_requests import propose_site_plan_request

    # Resolve the tier here as well as inside the propose helper — the response
    # quotes a price, and a client showing "$0/month" for a tier we could not
    # resolve would be worse than the refusal it replaced.
    tier = site_plans.site_scoped_tier(site_plans.canonical_site_tier_key(body.site_plan_key))
    # Partner-only rungs are sold through /partners/sell, never requested here.
    if tier is None or tier.partner_only:
        raise ValidationError(
            "sites.unknown_plan_tier",
            f"'{body.site_plan_key}' is not a plan a single site can be put on",
        )

    # The site's name for the Tray card, best-effort: an admin reading "Put
    # Acme Dental on the Staff plan" can decide; one reading a pocket id cannot.
    # A failure to resolve it must not block the request.
    # Imported locally, like every other cross-entity read in this module: the
    # sites service owns Site reads and reaches into pockets the same way.
    site_name = ""
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(body.pocket_id, ctx.user_id)
        site_name = str((pocket or {}).get("name") or "")
    except Exception:  # noqa: BLE001 — a nicer card is not worth failing the ask
        pass

    action_id = await propose_site_plan_request(
        workspace_id=ctx.workspace_id,
        pocket_id=body.pocket_id,
        site_plan_key=body.site_plan_key,
        requested_by=ctx.user_id,
        site_name=site_name,
    )
    return SitePlanRequestResponse(
        action_id=action_id,
        status="pending",
        site_plan_key=tier.key,
        monthly_price_usd=int(tier.monthly_price_usd or 0),
    )


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
    "/sites/by-pocket/{pocket_id}/html-armed-source",
    response_model=HtmlArmedSourceResponse,
)
async def get_html_armed_source_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> HtmlArmedSourceResponse:
    """Serve an html pocket's source with ``data-uid`` stamped on its editable leaves,
    plus the leaf manifest (HE-9).

    The html peer of ``/native-artifact``, and deliberately a DIFFERENT endpoint rather
    than a branch inside it. That one serves a BUILT, armed tree for shadow-rendering
    and is gated on ``has_native_edit_lane``, which html sits outside of on purpose —
    an html site's served artifact IS its source, so there is no build to arm. This is
    a parse and a splice over the source map: no Bun build, no Daytona, no artifact
    cache.

    It exists because select and write did not share an identity. The builder assembles
    the pocket's RAW source into a sandboxed srcdoc, so nothing in the previewed
    document carries a uid and a click resolved to a DOM-walk guess that
    ``apply_leaf_edits`` could never look up. Rendering THIS source instead closes that.

    Gated on ``fabric.write`` like ``/leaf-edits``: arming is a step in an edit flow,
    not a public read, and it returns the operator's unpublished draft content.

    A non-html pocket is a 422 (``pocket.not_html_site``); a missing / cross-tenant
    pocket is the pockets service's own 404 / 403. Callers RE-ARM after an edit rather
    than caching — spans shift under a splice, so a held manifest goes stale.
    """
    out = await sites_service.get_html_armed_source(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        pocket_id=pocket_id,
    )
    return HtmlArmedSourceResponse(
        pocket_id=pocket_id,
        source=out["source"],
        manifest=out["manifest"],
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
    works with no header. A pocket with no native edit lane is a 422 — svelte, react
    and html are served, ripple is not;
    a missing / access-denied pocket surfaces as a 404 / 403 (the pockets service
    raises it inside the service).

    DRAFT PREVIEW ORIGIN: every engine, html included, also answers ``preview_url`` —
    the draft's index.html on the cookieless preview host (``preview_origin.py``),
    ``None`` while a build is pending or failed. html never builds, so it is always
    ``build_status="none"`` with a URL and empty body/css. Ripple still 422s."""
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
    Tenant-scoped on ctx (fabric.write), like every sibling sites mutation.

    ``mode="rebuild"`` (IR-2a) takes the OTHER branch: the URL is read as a design
    reference, so nothing is minted and the 202 carries a ``brief_id`` for the
    captured design brief instead of a site. Both branches validate the URL
    identically and 422 before any write. The default is ``copy``, so a client
    that sends only ``url`` gets exactly the behaviour it always got.
    """
    if body.mode == "rebuild":
        queued = await import_service.regenerate_from_url(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, url=body.url
        )
    else:
        queued = await import_service.import_from_url(
            workspace_id=ctx.workspace_id, user_id=ctx.user_id, url=body.url
        )
        queued = {**queued, "mode": "copy"}
    return ImportFromUrlResponse(**queued)


@router.get("/sites", response_model=list[SiteResponse])
async def list_sites(ctx: RequestContext = Depends(request_context)) -> list[SiteResponse]:
    return await sites_service.list_for_workspace(ctx.workspace_id)


@router.get("/sites/slug-available", response_model=SlugAvailability)
async def slug_available(
    slug: str = Query(..., max_length=200),
    site_id: str | None = Query(None),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
    _rl: None = Depends(rate_limit_slug_check),
) -> SlugAvailability:
    """Is this address free? ``slug`` is raw input; the answer carries what it
    normalizes to, why it is not free (``invalid`` | ``reserved`` | ``taken`` |
    ``held``) and a free suggestion. With ``site_id`` that site's own current and
    pending address read as available; a site outside the workspace is a 404."""
    return await sites_service.check_slug_availability(
        workspace_id=ctx.workspace_id, raw=slug, site_id=site_id
    )


@router.put("/sites/{site_id}/slug", response_model=SiteResponse)
async def rename_site_slug(
    site_id: str,
    body: SlugRenameRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Reserve a new address for this site. It goes live on the NEXT publish, which
    moves the Worker and every custom domain to it; until then the site serves at its
    current address and the response shows the new one as ``slug_pending``.

    422 ``sites.slug_invalid``; 409 ``sites.slug_reserved`` / ``sites.slug_taken`` /
    ``sites.slug_held`` / ``sites.slug_unsupported_lane`` / ``sites.slug_needs_publish``
    / ``sites.slug_changed``; 429 ``sites.slug_rate_limited`` (3 a day per site); 404
    for a site outside the workspace."""
    return await sites_service.reserve_slug_rename(
        workspace_id=ctx.workspace_id, site_id=site_id, raw=body.slug
    )


@router.delete("/sites/{site_id}/slug/pending", response_model=SiteResponse)
async def cancel_site_slug_rename(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Cancel a waiting rename. Idempotent; the site keeps its current address."""
    return await sites_service.cancel_slug_rename(workspace_id=ctx.workspace_id, site_id=site_id)


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


@router.get("/sites/by-pocket/{pocket_id}/builds/latest", response_model=SiteBuildResponse)
async def latest_build_by_pocket(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteBuildResponse:
    """A project pocket's newest draft build (job id, status, ``preview_mode``), for
    polling: no realtime event exists for site builds. 404 when it never built, 422
    for a non-project pocket, the pockets service's 404 / 403 for no access."""
    result = await sites_service.project_latest_build(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id
    )
    return SiteBuildResponse(**result)


@router.get(
    "/sites/by-pocket/{pocket_id}/builds/{job_id}/log",
    response_model=SiteBuildLogResponse,
)
async def build_log_by_pocket(
    pocket_id: str,
    job_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteBuildLogResponse:
    """One project build's log (install, build and wrangler dry-run output), redacted
    and capped by the worker before it was stored. Owner/editor only, like the other
    by-pocket write routes: the log is the author's own build output. A job id that is
    not this pocket's is a 404."""
    result = await sites_service.project_build_log(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, pocket_id=pocket_id, job_id=job_id
    )
    return SiteBuildLogResponse(**result)


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


# ── Site data exports ────────────────────────────────────────────────────
#
# Declared BEFORE the "/sites/{site_id}/..." reads below so a literal "exports"
# segment can never be captured as a site id by a future route of that shape.
#
# The download is a STREAM, not a URL. Sites has no durable public address worth
# using for this — presigns expire, and the public asset rail is world-readable
# with an immutable year-long cache, which is right for a hero image and wrong for
# someone's booking records. So the bytes are re-authorised on every request
# instead: the storage key never leaves the backend and possession of a link grants
# nothing. Same shape as the workspace audit export.


@router.post("/sites/{site_id}/export", response_model=SiteExportResponse)
async def create_site_export(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteExportResponse:
    """Capture this site's data (its D1 tables and captured leads) for download.

    This is the precondition the delete cascade is gated on, so it either produces a
    bundle we can vouch for or FAILS — a dynamic site whose live data cannot be read
    is an error here, never an export reporting zero rows. The row is written before
    the build, so a crash leaves a visible failed export rather than nothing.
    """
    return await sites_service.create_site_export(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, site_id=site_id
    )


@router.get("/sites/exports", response_model=list[SiteExportResponse])
async def list_site_exports(
    site_id: str = Query(default=""),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> list[SiteExportResponse]:
    """This workspace's data exports, newest first; optionally one site's.

    Exports OUTLIVE the sites they came from — that is the point of them — so this
    lists rows whose ``site_id`` may no longer resolve to anything.
    """
    return await sites_service.list_site_exports(workspace_id=ctx.workspace_id, site_id=site_id)


@router.get("/sites/exports/{export_id}/download")
async def download_site_export(
    export_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> StreamingResponse:
    """Stream one READY export as a file attachment.

    A pending or failed export is a 400, never an empty download — zero bytes here
    would read as a site that had no data, which is the one thing this whole path
    exists to never say.
    """
    filename, chunks = await sites_service.open_site_export(
        workspace_id=ctx.workspace_id, export_id=export_id
    )
    return StreamingResponse(
        chunks,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="' + filename + '"'},
    )


@router.delete("/sites/{site_id}", response_model=SiteDeleteQueuedResponse, status_code=202)
async def delete_site(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteDeleteQueuedResponse:
    """Destroy a site and everything it owns. OWNER ONLY, and IRREVERSIBLE.

    202 AND NOT 204, which is the whole shape of this endpoint. The teardown is a
    durable job over an ordered cascade — cancel the billing, revoke the key, pull the
    routes, hostnames and Worker, then the D1, the bucket prefix and the dependent
    rows — and the site is STILL SERVING when this call returns. A 204 would be a
    simpler contract and a lie. Poll ``/delete-status`` from here.

    A DATA EXPORT IS FORCED FIRST, inside the job, before anything destructive runs.
    That is the entire recovery story for an action with no undo, and it is a hard
    stop rather than a best effort: an export that cannot be vouched for leaves the
    site completely untouched and settles the row at ``export:<cause>``.

    ``fabric.write`` like every sibling write, and then OWNER-ONLY on top of it in the
    service — a workspace member who may edit a site may not destroy it. The owner
    check is in the service rather than here because jobs, bus handlers and MCP tools
    reach services directly; see ``sites_service.start_site_delete``.
    """
    return await sites_service.start_site_delete(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, site_id=site_id
    )


@router.get("/sites/{site_id}/delete-status", response_model=SiteDeleteStatusResponse)
async def site_delete_status(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteDeleteStatusResponse:
    """How far a delete has got. 404 ONCE IT FINISHES — and that 404 is the success.

    There is no terminal "deleted" status to return, by construction: the cascade's
    last step removes the Site document, and ``delete_status`` is a field on that
    document. A client that treats this 404 as an error reports every successful
    delete as a broken one, so the shipped client reads it as completion instead.

    Owner-only like the delete itself. ``delete_reason`` names which step of a
    teardown stopped, which is operational detail about a site the reader may be able
    to see but not administer. A site in ANOTHER workspace is a 404 from the service's
    tenant-scoped load, which is a different answer from the 403 a non-owner inside
    the workspace gets, on purpose.
    """
    return await sites_service.site_delete_status(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, site_id=site_id
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


@router.get("/sites/{site_id}/project")
async def download_site_project(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> Response:
    """This site's project as a zip attachment — the copy its owner keeps.

    A PAID per-site capability. A site whose plan does not include it gets 402
    ``billing.project_download_not_entitled``, and the pre-check that lets the UI
    disable the button instead of provoking that is ``project_download`` on
    ``GET /sites/{site_id}/entitlements``. Do not gate the button on the workspace's
    source visibility instead — different plan, different resolver, and a paid site
    in a free workspace may download a project whose Code tab is hidden.

    A Ripple site is a 400 ``sites.project_not_downloadable``, never a zero-byte zip:
    an empty archive is indistinguishable from a site whose files vanished, and the
    archive is the one thing a customer cannot re-derive.

    ``Response`` and not ``StreamingResponse``, unlike the export download beside it.
    The archive is assembled whole in memory before any of it can be sent — it is one
    zip built from one Mongo document, capped at 16 MiB — so wrapping those bytes in
    an iterator would add a streaming interface over a payload that is already
    complete. The export streams because it reads pages from D1 as it goes.

    Tenant-scoped through the service's ``_load``, so a cross-tenant or missing site
    is a 404, and that check runs BEFORE the entitlement so a 402 can never confirm
    that another workspace's site exists.
    """
    assembled = await sites_service.download_site_project(
        workspace_id=ctx.workspace_id, site_id=site_id, user_id=ctx.user_id
    )
    return Response(
        content=assembled.data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="' + assembled.filename + '"',
            # Set explicitly because the whole payload is already in hand, so a
            # download UI can show real progress instead of an indeterminate spinner.
            "Content-Length": str(len(assembled.data)),
        },
    )


@router.get("/sites/{site_id}/analytics", response_model=SiteAnalyticsResponse)
async def site_analytics(
    site_id: str,
    window: str = Query(default="7d", description="24h | 7d | 30d | 90d"),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteAnalyticsResponse:
    """This site's visitor numbers over one window, for the builder's Analytics panel.

    ``status`` is the field to read FIRST, and the response is shaped so a client that
    ignores it shows blanks rather than confident zeros. Three situations produce an
    empty panel and they are three different sentences to the customer: the plan does
    not include analytics, it does but nothing has been published since (so nothing was
    ever recorded), or a counter is up and genuinely nobody visited. Only the last is
    about their traffic.

    A read that FAILS is an error response, never a status — see the service. A client
    that defaults an unknown status to "no data" would otherwise render a Cloudflare
    outage as a quiet week.

    ``window`` is validated against a closed set (a bad one is a 422), which is a SQL
    control and not only input hygiene: the Analytics Engine endpoint takes raw text
    with no parameter binding. Tenant-scoped like every sibling per-site read, so a
    cross-tenant or missing site is a 404.
    """
    return await sites_service.site_analytics(
        workspace_id=ctx.workspace_id, site_id=site_id, window=window
    )


@router.patch("/sites/{site_id}/metadata", response_model=SiteResponse)
async def update_site_metadata(
    site_id: str,
    body: SiteMetadataUpdate,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Rename a site or edit its one-line description.

    Three-way: a field the caller omits is left alone, and an explicit empty string
    clears it — which is how the form deletes a description without a second
    endpoint. A blank NAME is refused (422) rather than accepted, because the name is
    both the site's identity in the gallery and the string the delete confirmation
    asks the owner to type back.

    Tenant-scoped like every sibling per-site write; a missing or cross-tenant site
    is a 404.
    """
    return await sites_service.update_site_metadata(
        workspace_id=ctx.workspace_id, site_id=site_id, body=body
    )


@router.patch("/sites/{site_id}/branding", response_model=SiteResponse)
async def update_site_branding(
    site_id: str,
    body: SiteBrandingUpdate,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Show or hide the "Built with PocketPaw" badge on this site (VS-3).

    ``badge_hidden=true`` needs a plan that grants badge removal: a site that is not
    entitled gets 402 ``billing.badge_removal_not_entitled`` and nothing is written.
    ``badge_hidden=false`` is always accepted. Repeating the stored value is a no-op.

    Authorized like ``PATCH /sites/{id}/metadata``: ``fabric.write`` in the caller's
    workspace, tenant-scoped, so a missing or cross-tenant site is a 404.
    """
    return await sites_service.update_site_branding(
        workspace_id=ctx.workspace_id, site_id=site_id, body=body
    )


@router.patch("/sites/{site_id}/ai-visibility", response_model=SiteResponse)
async def update_site_ai_visibility(
    site_id: str,
    body: SiteAiVisibilityUpdate,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteResponse:
    """Let AI-training crawlers use this site, or not (AV-1).

    ``ai_training_allowed`` changes only the training lines of the robots.txt the
    next publish writes; search and assistant crawlers are always allowed. Authorized
    like ``PATCH /sites/{id}/branding``; a missing or cross-tenant site is a 404.
    """
    return await sites_service.update_site_ai_visibility(
        workspace_id=ctx.workspace_id, site_id=site_id, body=body
    )


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
    x_paw_money_units: str | None = Header(default=None, alias=MONEY_UNITS_HEADER),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteClientResponse:
    """Log one manual receipt against the site's client. This records that the
    owner was paid — it does NOT charge anyone, and it is unrelated to the owner's
    own subscription with us. Returns the whole updated client record so the caller
    re-renders from one response instead of splicing the new row in locally.
    ``X-Paw-Money-Units: iso4217`` marks the amount as minor units; without it the
    amount is read as a legacy client's major × 100 and converted."""
    return await sites_service.record_site_invoice(
        workspace_id=ctx.workspace_id,
        site_id=site_id,
        body=body,
        minor_units=client_sends_minor_units(x_paw_money_units),
    )


# ── The public asset rail ───────────────────────────────────────────────
#
# Gated exactly like the other pocket-scoped writes here: fabric.write to add or
# remove, fabric.read to list. Tenancy is not left to the gate alone — every key
# is built from ``ctx.workspace_id``, so a caller cannot name another workspace's
# object even with a valid token for their own.


def _asset_store_or_503():
    """Return the configured store, or 503 with a message an operator can act on."""
    from pocketpaw_ee.sites.public_assets import public_asset_store

    store = public_asset_store()
    if store is None:
        # Deliberately NOT a fallback to the private adapter. A link that 403s for
        # every visitor is worse than a clear refusal, because it fails later and
        # looks like a broken site rather than a missing setting.
        raise HTTPException(
            503,
            "Public asset storage is not configured for this deployment "
            "(set POCKETPAW_UPLOAD_ADAPTER=s3 and S3_PUBLIC_BUCKET).",
        )
    return store


@router.post("/sites/by-pocket/{pocket_id}/assets", response_model=SiteAssetResponse)
async def upload_site_asset(
    pocket_id: str,
    file: UploadFile = File(...),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteAssetResponse:
    """Store one image on the public bucket and return its durable URL.

    Only PNG/JPEG/GIF/WebP are accepted, and acceptance is decided by MAGIC BYTES,
    never by the part's Content-Type — on a public origin a caller who can label
    arbitrary bytes ``image/png`` can host arbitrary content under our name. SVG is
    refused for the same reason even though it is an image: it executes script.

    Like ``import_site`` above, the cap here gates PROCESSING, not ingress —
    Starlette spools the whole body before this runs, so bounding the raw request
    is the fronting proxy's job. Oversized → 413; anything the rail refuses → 400
    with a message written to be shown to the user verbatim.
    """
    from pocketpaw_ee.sites.public_assets import MAX_ASSET_BYTES, PublicAssetError

    store = _asset_store_or_503()

    data = await file.read(MAX_ASSET_BYTES + 1)
    if len(data) > MAX_ASSET_BYTES:
        raise HTTPException(
            413,
            f"That image exceeds the {MAX_ASSET_BYTES // (1024 * 1024)}MB upload cap.",
        )

    try:
        asset = await store.put(
            data,
            filename=file.filename or "asset",
            workspace_id=ctx.workspace_id,
            pocket_id=pocket_id,
        )
    except PublicAssetError as exc:
        # The message is authored for a human and carries no internal detail.
        raise HTTPException(400, str(exc)) from exc

    return SiteAssetResponse(
        key=asset.key,
        url=asset.url,
        mime=asset.mime,
        size=asset.size,
        filename=asset.filename,
    )


@router.get("/sites/by-pocket/{pocket_id}/assets", response_model=SiteAssetListResponse)
async def list_site_assets(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteAssetListResponse:
    """List every image uploaded for this site, newest naming first by filename."""
    store = _asset_store_or_503()
    assets = await store.list(workspace_id=ctx.workspace_id, pocket_id=pocket_id)
    return SiteAssetListResponse(
        assets=[
            SiteAssetResponse(key=a.key, url=a.url, mime=a.mime, size=a.size, filename=a.filename)
            for a in assets
        ]
    )


@router.delete("/sites/by-pocket/{pocket_id}/assets", status_code=204)
async def delete_site_asset(
    pocket_id: str,
    body: SiteAssetDeleteRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> None:
    """Delete one asset, refusing any key outside this site's own prefix.

    The key arrives from the client, so the prefix check in the store is what
    stands between this and an arbitrary cross-tenant object delete. It is
    enforced there rather than here so a non-HTTP caller inherits it too.
    """
    from pocketpaw_ee.sites.public_assets import PublicAssetError

    store = _asset_store_or_503()
    try:
        await store.delete(workspace_id=ctx.workspace_id, pocket_id=pocket_id, key=body.key)
    except PublicAssetError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.get("/sites/import/brief/{brief_id}", response_model=ImportBriefStatusResponse)
async def get_import_brief(
    brief_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ImportBriefStatusResponse:
    """Where a rebuild capture has got to (IR-2b).

    The rebuild 202 returns immediately with a ``brief_id`` and the crawl runs in
    the background, so the client polls this to learn when there is a brief to
    generate from. Four distinguishable states, because a client that cannot tell
    ``queued`` from ``failed`` spins forever on a dead capture.

    Tenant-scoped on ctx: a brief id from another workspace is not found here, not
    readable. A read, so no ``fabric.write`` — same shape as the sibling listings.
    """
    return await import_service.read_design_brief(workspace_id=ctx.workspace_id, brief_id=brief_id)


# --- Wave 3: transferring a site to another workspace ----------------------
#
# Appended at end-of-file, and gated on BOTH ends. The send half is
# ``fabric.write`` in the SOURCE workspace plus an owner check in the service; the
# receive half is ``fabric.write`` in the DESTINATION plus a membership check read
# from the accepting user's own record. Neither half alone is sufficient, which is
# the whole reason this is an offer and an accept rather than one call.


@router.post("/sites/{site_id}/transfer", response_model=SiteTransferResponse)
async def offer_site_transfer(
    site_id: str,
    body: SiteTransferOfferRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteTransferResponse:
    """Offer this site to another workspace. Nothing moves until it is accepted.

    Tenant-scoped through the service's ``_load``, so the caller can only offer a
    site their own workspace owns. Owner-only beyond that, and refused outright when
    an admin has turned off outbound transfers — a site leaving takes its leads with
    it, which makes this a data-egress control rather than a preference.
    """
    wire = await sites_service.offer_site_transfer(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        site_id=site_id,
        destination_workspace_id=body.destination_workspace_id,
    )
    return _transfer_response(wire)


@router.delete("/sites/{site_id}/transfer", response_model=SiteTransferResponse)
async def cancel_site_transfer(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteTransferResponse:
    """Withdraw an offer that has not been accepted yet."""
    wire = await sites_service.cancel_site_transfer(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        site_id=site_id,
    )
    return _transfer_response(wire)


@router.get("/sites/transfers/incoming", response_model=SiteTransferListResponse)
async def list_incoming_site_transfers(
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> SiteTransferListResponse:
    """Sites other workspaces have offered to this one.

    The one sites read not anchored on the caller's ``workspace`` — it cannot be,
    since an offered site still belongs to the sender. ``transfer_to_workspace`` is
    the tenant filter instead, and only an owner of the sending side can write it,
    so a workspace sees exactly what was addressed to it.

    Starlette matches routes in REGISTRATION order, not by specificity, so a
    literal path registered after a parameterised one is not automatically safe.
    This one is safe because nothing overlaps: ``/sites/{site_id}/transfer`` would
    only swallow this if the final segment read ``transfer``, and it reads
    ``incoming``. Adding a ``/sites/{site_id}/incoming`` later would break it.
    """
    rows = await sites_service.list_incoming_site_transfers(workspace_id=ctx.workspace_id)
    return SiteTransferListResponse(transfers=[_transfer_response(r) for r in rows])


@router.post("/sites/transfers/{site_id}/accept", response_model=SiteTransferResponse)
async def accept_site_transfer(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> SiteTransferResponse:
    """Accept a site offered to this workspace, and take ownership of it.

    Deliberately NOT under ``/sites/{site_id}/`` — every route on that prefix is
    tenant-scoped to the caller's workspace, and this one addresses a site that
    still belongs to somebody else. Putting it on its own prefix keeps that
    difference visible at the URL rather than buried in a service function.

    The receiving guard that matters is not this dependency: ``fabric.write`` says
    the caller may write in the workspace the header names, and the service
    re-checks that they are actually a MEMBER of it against their own user record.
    A header is a request, not a credential.
    """
    wire = await sites_service.accept_site_transfer(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        site_id=site_id,
    )
    return _transfer_response(wire)


def _transfer_response(wire: dict) -> SiteTransferResponse:
    """Map the service's wire dict onto the response model.

    Hand-written rather than ``model_validate`` because the service speaks camelCase
    (the shape every other sites read returns) and the DTO is snake_case.
    """
    return SiteTransferResponse(
        site_id=wire.get("siteId", ""),
        name=wire.get("name", ""),
        url=wire.get("url", "") or "",
        from_workspace_id=wire.get("fromWorkspaceId", ""),
        to_workspace_id=wire.get("toWorkspaceId", "") or "",
        offered_by=wire.get("offeredBy", "") or "",
        offered_at=wire.get("offeredAt"),
        status=wire.get("status", "none"),
    )


# --- SF-8: proving the workspace controls an origin -------------------------
#
# These two exist so a later slice can crawl a customer's OWN pages without
# becoming a crawler-for-hire. Both are workspace-scoped writes (``fabric.write``)
# under the router's sites plan gate, like every sibling mutation: a claim mints a
# secret and a verification flips a durable permission, so neither is a read.
#
# NOTHING IS MINTED AND NOTHING IS CRAWLED HERE. A claim writes one
# ``SiteOriginClaim`` row; a verification updates it or raises. No Site document,
# no import, no crawl is reachable from either — the crawl is a separate slice
# that asks ``ownership.verified_origin`` first.


@router.post("/sites/origins/claims", response_model=OriginClaimResponse, status_code=201)
async def claim_site_origin(
    body: OriginClaimRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> OriginClaimResponse:
    """Issue this workspace's verification token for a domain it says it owns.

    The token is bound to (workspace, host) and is the ONLY response shape that
    carries it. Re-claiming a pending domain re-mints the token; re-claiming one
    that is already verified returns the verified row untouched, because
    re-issuing there would unprove a live binding.

    A malformed or non-public host (URL syntax, a single label, any literal IP —
    loopback and link-local included) is a 422 raised during normalization, before
    any row is written and before any DNS lookup.
    """
    claim = await ownership.claim_origin(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, host=body.host
    )
    return OriginClaimResponse(
        host=claim.host,
        token=claim.token,
        status=claim.status,
        expires_at=claim.expires_at,
        well_known_url=f"https://{claim.host}{ownership.WELL_KNOWN_PATH}",
        meta_tag=f'<meta name="{ownership.META_NAME}" content="{claim.token}">',
        verified_at=claim.verified_at,
        method=claim.method,
    )


@router.post("/sites/origins/verify", response_model=OriginVerificationResponse)
async def verify_site_origin(
    body: OriginClaimRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> OriginVerificationResponse:
    """Read the token back off the claimed domain and record the origin verified.

    The host travels in the BODY rather than the path deliberately: a hostname in
    a path segment invites percent-encoding bugs on exactly the input whose
    normalization is a security boundary here.

    The service fetches through the one SSRF-hardened fetch, pinned to the claimed
    host. A failure — nothing published, the wrong token, an expired claim, an
    unreachable domain — is an error response and writes nothing at all.
    """
    claim = await ownership.verify_origin(workspace_id=ctx.workspace_id, host=body.host)
    # A fresh proof is what lets a connected site's card read the customer's page,
    # so every connected site on this host refreshes its title, icon and picture.
    await sites_service.schedule_connected_cards_for_origin(ctx.workspace_id, claim.host)
    return OriginVerificationResponse(
        host=claim.host,
        status=claim.status,
        method=claim.method,
        verified_at=claim.verified_at,
    )


# --- SF-13: the foreign concierge over HTTP --------------------------------
#
# Four slices built the foreign concierge — ownership proof, a charged mint, an
# idempotent bind, knowledge grounding — and none of them reached the wire, so
# the feature was complete as a library and unreachable as a product. These are
# the routes the setup UI calls.
#
# THE BIND SPENDS MONEY, which is what makes its gate different from every other
# mutation on this router. ``fabric.write`` says the caller may write in this
# workspace; ``sites.buy_plan`` (ADMIN, refusal code
# ``sites.plan_purchase_forbidden``) says they may commit it to a recurring
# charge. Both are required, which is the pair ``publish_site`` already uses —
# the difference is that a publish is usually free, so it ASKS the second
# question and lets the service decide, while a first bind is always a $19/month
# purchase and there is nothing to decide. A member who may not buy still gets
# the GET, so "does one already exist" never needs an admin.
#
# IDEMPOTENCE IS THE SERVICE'S, AND THIS LAYER MUST NOT HELP. The guarantee is a
# derived primary key (a duplicate insert fails BEFORE the debit) plus an
# in-process lock. So nothing here mints an id, retries a ``DuplicateKeyError``,
# or catches a conflict and re-calls: each of those would turn one purchase into
# two. The route calls ``bind_foreign_concierge`` — the resolve-or-buy layer —
# and never ``mint_foreign_site``, which buys a month unconditionally.
#
# THE ERROR VOCABULARY IS THE SERVICE'S TOO, and the mapping is the standard
# envelope rather than anything written here: ``sites.origin_unverified`` and
# ``sites.origin_verification_stale`` are both 403 but are DIFFERENT codes,
# because "you never proved you own this domain" and "your proof is older than
# 30 days" need different sentences from the owner, and a panel that collapsed
# them would tell a customer to re-verify a domain they never claimed.
#
# WHAT IS NOT HERE, BECAUSE IT ALREADY EXISTS: the grounding result. GET/POST
# ``/paw-bar/admin/site/{site_id}/knowledge`` already serve the article count,
# the last sync stamp, the sync error and an owner-triggered re-sync for any Site
# in the workspace, a foreign row included. This response carries ``site_id`` so
# the panel can call them; a second read of the same fields would be a second
# thing to keep in step.

_FOREIGN_CONCIERGE_PATH = "/sites/by-pocket/{pocket_id}/foreign-concierge"


async def _assert_pocket_readable(pocket_id: str, user_id: str) -> None:
    """Refuse a caller who cannot reach this pocket, before the Site is looked up.

    ``foreign_site_for_pocket`` filters on workspace and ``foreign_origin`` and
    nothing else, so it answers "is there a concierge here" rather than "may YOU
    ask". Both of this surface's guards are MEMBER — ``fabric.read`` and
    ``fabric.write`` say what a call INTENDS, not who is privileged to make it —
    so without this a member who is refused a private pocket everywhere else in
    the product could still read its concierge's ``site_key`` and embed snippet,
    and could rotate that key out from under a live page.

    Raises whatever the pockets service raises: Forbidden
    ``pocket.access_denied`` for a pocket this user may not read, NotFound for one
    that does not exist. Called for the side effect only — the doc it returns is
    not this surface's business.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    await pockets_service.get(pocket_id, user_id)


async def _foreign_concierge_response(site: Any) -> ForeignConciergeResponse:
    """Render one foreign Site as the panel's view of its concierge.

    The snippet comes from ``embed.concierge_snippet`` rather than being formatted
    here, so the five gates that decide whether a site has earned a bar keep
    exactly one definition. Its ``concierge_entitled`` half is asked of
    ``site_keys.concierge_plan_entitled`` — the plan half of the predicate the
    public seams use — so this panel and the visitor's first message cannot
    disagree about the plan. The response carries that half, the switch and the
    create marker separately, so the panel can say which one is missing.

    FAILURE-SOFT ON THE BAR, NOT ON THE ROW. paw_bar is imported lazily (the
    sites service must load in a deployment that does not carry it) and anything
    escaping the widget lookup leaves the snippet empty with a log line, because a
    row that is paid for and readable matters more than the panel's copy button.
    """
    from pocketpaw_ee.cloud.auth.site_keys import (
        concierge_available,
        concierge_exists,
        concierge_plan_entitled,
    )
    from pocketpaw_ee.sites import foreign_grounding

    workspace_id = str(getattr(site, "workspace", "") or "")
    pocket_id = str(getattr(site, "pocket_id", "") or "")
    site_key = str(getattr(site, "signed_key", "") or "")
    from pocketpaw_ee.cloud.billing.enforcement import load_partner

    partner = await load_partner(workspace_id)
    available = bool(concierge_available(site, partner=partner))
    entitled = bool(concierge_plan_entitled(site, partner=partner))
    enabled = bool(getattr(site, "concierge_enabled", False))
    exists = concierge_exists(site)

    snippet = ""
    widget_id = ""
    agent_id = ""
    try:
        from pocketpaw_ee.paw_bar import embed
        from pocketpaw_ee.paw_bar.agent_provisioning import site_widget

        widget = await site_widget(pocket_id, workspace_id)
        widget_id = str(getattr(widget, "id", "") or "") if widget else ""
        agent_id = str(getattr(widget, "agent_id", "") or "") if widget else ""
        snippet = await embed.concierge_snippet(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            site_key=site_key,
            api_base=sites_service._capture_base(),
            concierge_enabled=enabled,
            # The plan half, from the one helper that owns the rule (it also
            # honours the sites-billing flag, which a re-expression here would
            # have to remember).
            concierge_entitled=entitled,
            # CR-12: a bought connection has no concierge until its owner creates one.
            concierge_exists=exists,
        )
    except Exception:  # noqa: BLE001 — the row is real whether or not the bar is
        import logging

        logging.getLogger(__name__).warning(
            "sites.foreign_concierge: could not resolve the bar for site %s",
            str(getattr(site, "id", "?")),
            exc_info=True,
        )

    origins: list[ForeignConciergeOrigin] = []
    for host in list(getattr(site, "allowed_origins", None) or []):
        record = await ownership.verified_origin_record(workspace_id, host)
        origins.append(
            ForeignConciergeOrigin(
                host=host,
                verified=record is not None,
                verified_at=getattr(record, "verified_at", None),
                # The same 30-day rule the bind and the crawl apply, asked of the
                # same function — so the panel cannot say "verified" about a proof
                # the next grounding run will refuse.
                verification_fresh=foreign_grounding.verification_is_fresh(record),
            )
        )

    return ForeignConciergeResponse(
        exists=True,
        site_id=str(getattr(site, "id", "") or ""),
        pocket_id=pocket_id,
        name=str(getattr(site, "name", "") or ""),
        site_key=site_key,
        embed_snippet=snippet,
        widget_id=widget_id,
        agent_id=agent_id,
        origins=origins,
        plan_tier=str(getattr(site, "plan_tier", "") or ""),
        subscription_status=str(getattr(site, "subscription_status", "") or "none"),
        renewal_date=getattr(site, "renewal_date", None),
        concierge_available=available,
        concierge_entitled=entitled,
        concierge_enabled=enabled,
        concierge_exists=exists,
    )


@router.post(
    _FOREIGN_CONCIERGE_PATH,
    response_model=ForeignConciergeResponse,
    dependencies=[Depends(require_action_any_workspace("sites.buy_plan"))],
)
async def bind_pocket_foreign_concierge(
    pocket_id: str,
    body: ForeignConciergeBindRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ForeignConciergeResponse:
    """Create or return the one foreign concierge for this pocket.

    The first call debits $19/month from the workspace credit wallet; every call
    after it returns the same concierge and charges nothing. A repeat is a 200
    with the same ``site_id``, not a 409 — a double-clicked button is the case
    this endpoint is shaped around.

    Every refusal happens before a row exists and before money moves: a pocket
    the caller cannot access, or one that belongs to a different workspace (403
    ``pocket.access_denied`` for both — the same code, so a guessed id is not
    told which), an origin the workspace has not proved it controls (403
    ``sites.origin_unverified``), a
    proof older than 30 days (403 ``sites.origin_verification_stale``), no usable
    origin (422 ``sites.origin_required``) and a wallet that cannot cover the
    month (402 ``credits.insufficient``). The last one deletes the unpaid row it
    had just inserted, so a 402 leaves nothing behind either.
    """
    site = await sites_service.bind_foreign_concierge(
        workspace_id=ctx.workspace_id,
        pocket_id=pocket_id,
        owner=ctx.user_id,
        allowed_origins=body.allowed_origins,
        name=body.name,
    )
    return await _foreign_concierge_response(site)


@router.get(_FOREIGN_CONCIERGE_PATH, response_model=ForeignConciergeResponse)
async def get_pocket_foreign_concierge(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> ForeignConciergeResponse:
    """What this pocket's foreign concierge currently is, if it has one.

    ``fabric.read`` and not the bind's admin gate, deliberately: a member who may
    not commit the workspace to a charge still needs to see that the concierge
    exists, what its snippet is, and which of its origins have gone stale.

    A pocket with no foreign concierge is ``exists: false``, not a 404. The
    lookup is ``foreign_site_for_pocket``, which filters on ``foreign_origin``, so
    a pocket that also has a PUBLISHED site never has that site reported here.

    A pocket that does not EXIST is a 404 rather than ``exists: false``, because
    the pocket gate below runs ahead of the site lookup. That is the honest answer
    — "this pocket has no concierge" is a statement about a pocket — and the
    alternative would have this route describe pockets the caller cannot see.
    """
    # THE READ'S POCKET GATE. Reading a concierge hands back its ``site_key`` and
    # the snippet that uses it; neither should reach a caller who cannot open the
    # pocket the concierge speaks for.
    await _assert_pocket_readable(pocket_id, ctx.user_id)
    site = await sites_service.foreign_site_for_pocket(ctx.workspace_id, pocket_id)
    if site is None:
        return ForeignConciergeResponse(exists=False, pocket_id=pocket_id)
    return await _foreign_concierge_response(site)


@router.post(f"{_FOREIGN_CONCIERGE_PATH}/rotate-key", response_model=ForeignConciergeResponse)
async def rotate_pocket_foreign_concierge_key(
    pocket_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> ForeignConciergeResponse:
    """Retire this concierge's embed key and issue a new one.

    ``fabric.write`` rather than the bind's ``sites.buy_plan``: a rotation is not
    a repurchase — same row, same tier, same renewal date — and gating a leak
    response behind an admin would leave a member who can see the leak unable to
    act on it.

    The response carries the NEW snippet, which is the whole point: the old key
    stops resolving the moment this returns, so the owner's page is serving a
    dead credential until they paste this one in. 404 when the pocket has no
    foreign concierge to rotate, and 403 when the caller cannot open the pocket.
    """
    # THE ROTATION'S POCKET GATE, and the loudest of the three. A rotation kills
    # the live key the instant it lands, so without this a member who is refused
    # the pocket can still take a customer's published page offline.
    await _assert_pocket_readable(pocket_id, ctx.user_id)
    site = await sites_service.rotate_foreign_concierge_key(
        workspace_id=ctx.workspace_id, pocket_id=pocket_id
    )
    return await _foreign_concierge_response(site)


@router.post(f"{_FOREIGN_CONCIERGE_PATH}/rebind", response_model=ForeignConciergeResponse)
async def rebind_pocket_foreign_concierge(
    pocket_id: str,
    body: ForeignConciergeRebindRequest,
    ctx: RequestContext = Depends(request_context),
    caller: Any = Depends(require_action_any_workspace("fabric.write")),
) -> ForeignConciergeResponse:
    """Point this concierge's bar at a different agent, leaving the row alone.

    Nothing about the purchase moves: the embedded ``signed_key`` keeps
    resolving, the tier stays bought and the renewal date stays where it was, so
    swapping the answering agent never costs the buyer their credential or their
    month. A non-admin may only name an agent that ALREADY answers for a
    foreign concierge in this workspace; anything else is 403
    ``sites.agent_not_published``, an agent in another tenant included. The
    service owns that rule and its reasoning; the role is the only part this
    layer knows. For an ADMIN the rule is relaxed, and a cross-tenant
    ``agent_id`` is then a 404 from inside the funnel, deliberately
    indistinguishable from an agent that does not exist.

    The row is re-read for the response rather than returned by the rebind, which
    hands back the bound agent id; ``exists: false`` here means the concierge was
    deleted between the two, not that the rebind failed.
    """
    # THE REBIND'S POCKET GATE. A rebind decides which agent answers the public,
    # so it is at least as privileged as reading the pocket it answers for.
    await _assert_pocket_readable(pocket_id, ctx.user_id)
    # The role, read off the membership the guard above already resolved rather
    # than re-asked through ``check_workspace_action`` — that function AUDITS a
    # denial, and a member doing an ordinary rebind has not been denied anything.
    from pocketpaw_ee.guards.actions import WorkspaceRole
    from pocketpaw_ee.guards.deps import resolve_workspace_role

    role = resolve_workspace_role(caller, ctx.workspace_id or "")
    await sites_service.rebind_foreign_concierge(
        workspace_id=ctx.workspace_id,
        pocket_id=pocket_id,
        agent_id=body.agent_id,
        widget_id=body.widget_id,
        caller_is_admin=role.level >= WorkspaceRole.ADMIN.level,
    )
    # Named for what it is — a re-read after the write — rather than sharing the
    # read endpoint's ``site``. The two blocks are otherwise identical text, and a
    # mutation anchored on either one would then match both, which is how a plan
    # ends up unable to say which arm it covers.
    refreshed = await sites_service.foreign_site_for_pocket(ctx.workspace_id, pocket_id)
    if refreshed is None:
        return ForeignConciergeResponse(exists=False, pocket_id=pocket_id)
    return await _foreign_concierge_response(refreshed)
