# ee/pocketpaw_ee/sites/service.py — Sites control-plane orchestration. Sole
# owner of Site writes.
#
# Updated 2026-06-01 (Phase 4 — chat→create-site): added publish_pocket(), the
# shared "publish a pocket by id" path. It reads the pocket's rippleSpec + theme
# via pockets_service (logic lifted verbatim from the router) and delegates to
# publish(). Both the REST endpoint (POST /sites/publish) and the new in-process
# MCP tool (mcp__pocketpaw_sites_manager__publish) call it, so the chat and HTTP
# surfaces share ONE code path that reads the pocket, derives the theme, and
# names the site.
#
# Updated 2026-06-04 (feat/sites-svelte-engine — Paw Sites "Svelte track"):
# publish_pocket() now also reads the pocket's ``engine`` ("ripple" | "svelte")
# and, for svelte sites, its ``source`` map from the wire dict, and forwards
# both to publish() → generator.build(), which forks STAGE 2 on the engine
# (design spec §4.2). Ripple pockets read ``engine="ripple"`` / ``source=None``
# and behave exactly as before. ``ripple_spec`` is now optional on publish()
# (svelte sites have none).
#
# publish() runs: mint site id + signed key → generate +
# smoke-gate the SvelteKit app (generator_client) → PUT the Worker into the WfP
# dispatch namespace → persist the Site. add_domain()/domain_status() drive
# Cloudflare for SaaS. The generator + Cloudflare client + bundle reader are
# injectable so the orchestration is unit-testable without Bun/workerd/CF.
#
# Tenancy: workspace_id is a required parameter on every function; reads filter
# on it. The signed key is minted per site (reused by the capture endpoint).
#
# CF creds (account id + API token + zone) come from env in v1 (PAW_CF_*); the
# client reads them from settings — it does NOT store per-tenant CF creds in v1
# (see the plan's Phase 2 note + cloudflare_client.py). When per-tenant storage
# lands, the token follows the encrypt-before-Mongo pattern other cloud
# credentials use (_core/crypto.encrypt_json) — never logged, never plaintext.
#
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
#
# Updated 2026-05-30 (security hardening, H3): _load now guards the
# ObjectId(site_id) cast — a malformed, attacker-supplied site_id raised
# bson.errors.InvalidId, which the cloud error handler (CloudError-only) let
# escape as an unhandled 500. The cast is wrapped so a bad id surfaces as a 404
# NotFound. add_domain / domain_status both route through _load, so this covers
# every authed path that casts a caller-supplied site_id.
#
# Updated 2026-05-30 (follow-up item 4): added list_domains() — a tenant-scoped
# read of the Site doc's domains list (hostname + status + cname_target), backing
# GET /sites/{site_id}/domains so the Domains tab can rehydrate on reload. It
# routes through _load, so it inherits the same tenant scoping + malformed-id
# guard as the other authed domain paths (no Cloudflare call).
#
# Updated 2026-06-01 (Phase 2 — lead capture lands without manual Mongo edits):
# publish() now seeds the Site with a DEFAULT event_mapping and default
# allowed_origins so a freshly published site can receive a basic
# {full_name, phone, email, message} lead out of the box. Before this, publish()
# left event_mapping={} (every capture dropped "no_mapping") and
# allowed_origins=[] (origin_allowed fails closed → every POST 403'd), so a lead
# could only land after hand-editing the Site doc (the dentist e2e did exactly
# that). The default mapping is keyed on form_type "lead" — the same constant the
# generated /api/submit endpoint sends. add_domain() now also appends the custom
# hostname to allowed_origins, so connecting a domain authorizes the site's own
# origin with no extra step.
#
# Updated 2026-06-01 (Phase 3 — LOCAL fake-deploy so publish works with zero
# Cloudflare creds): publish() now has an additive LOCAL deploy branch. When CF
# creds are absent (no PAW_CF_ACCOUNT_ID) OR PAW_SITES_LOCAL=1, and no CF client
# was injected, publish() SKIPS the Cloudflare upload and instead persists the
# built static site under ~/.pocketpaw/sites/<site_id>/ and serves it over HTTP
# via a per-process static server (local_server.py). The Site's ``url`` is set to
# that localhost URL so the SiteResponse carries a real openable address for the
# cmux smoke. The REAL Cloudflare path is unchanged and stays the default when
# creds ARE present (or a CF client is injected, e.g. by tests). PROD TODO: local
# mode is a dev shim — production always takes the CF path.
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): thread
# ``builder_origin`` through the publish path so a svelte Paw Site stays
# editable. publish()/publish_pocket() forward it to generator.build() (it rides
# siteConfig.builderOrigin, which SE-1's generator gates the edit-bridge on) and
# store it on the Site doc. edit_svelte_component() recovers the stored origin
# from the pocket's current Site (via _latest_site_for_pocket) and re-applies it,
# so a component edit does not strip the bridge. make_site_editable() republishes
# a pocket as editable (builder_origin set, defaulting to PAW_SITES_BUILDER_ORIGIN)
# and backs POST /sites/by-pocket/{pocket_id}/editable.
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2): added
# edit_svelte_component() — rewrite ONE file of a svelte Paw Site pocket's
# ``source`` map and safely republish. It delegates the Pocket write to the
# pockets service (set_svelte_source_file — entity isolation), then calls
# publish_pocket() to regenerate + smoke-gate + redeploy. publish() smoke-gates
# BEFORE it deploys, so a broken edit never reaches the live deploy; on
# SmokeGateFailed this function ALSO rolls the persisted source back to its prior
# contents and re-raises, so neither the deploy nor the stored source is left
# broken. This is the chat-agent surface for a targeted component edit, exposed
# beside create_landing_site / create_svelte_site / publish on the
# pocketpaw_sites_manager MCP server.
#
# Updated 2026-06-03 (Sites backend fixes A+B): (A) added site_pocket_ids() — the
# set of pocket_ids that have a Site in a workspace, so the /pockets gallery can
# exclude already-published pockets WITHOUT the pockets service importing the Site
# model (entity isolation: the Site read stays here, the sole owner of Site
# reads). (B) publish() now resolves a blank ``name`` to the source pocket's own
# display name (via the pockets service's PUBLIC ``get`` — no Beanie import),
# falling back to "Untitled site" only when the pocket has no name. This makes the
# publish schema's "defaults to the pocket's own name" promise true at the
# source-of-truth layer, so sites no longer land unnamed when the caller omits a
# name. The resolved name flows into BOTH the generated site ``title`` and the
# stored ``Site.name``.
#
# Updated 2026-06-17 (pocketpaw#1345 backend half — by-pocket preview + status):
# added preview_pocket() and pocket_status(), the two by-pocket reads the #432
# frontend already calls (the backend half of #1345 never landed on dev, so every
# Preview-tab fetch 404'd and the builder showed "Nothing to preview yet").
# preview_pocket() reads the source pocket via the pockets service and returns its
# DRAFT content — the rippleSpec for a ripple pocket, the {path: contents} source
# map for a svelte pocket — reusing publish_pocket()'s pocket-read + engine logic
# so the preview matches what publish would build. pocket_status() derives
# draft/published + is_live from the Site deployment doc for the pocket
# (tenant-scoped on ``workspace``, via the model's compound index): no Site doc =
# draft / not live; a Site doc = published with is_live following ``deployed``.
# Updated 2026-06-17 (feat/sites-local-reserve — local sites die on restart):
# added reserve_local_sites(). LOCAL deploy mode (Phase 3) serves sites from a
# per-process static server bound to an EPHEMERAL port that is only started
# during publish(). After a backend restart the server is gone and every stored
# ``url`` (http://127.0.0.1:<old-port>/<id>/) is dead, even though the built
# files survive under sites_home()/<id>/. reserve_local_sites() (re)starts the
# shared server via local_server.ensure_server() and rewrites every deployed
# site's ``url`` to the fresh live base, so prior local sites become openable
# again. It is a no-op outside local mode (the real CF path owns its own URLs)
# and skips sites whose files are not on disk. The cloud boot hook calls it
# unscoped so a restart auto-re-serves all sites; POST /sites/reserve calls it
# workspace-scoped for an explicit "re-serve" action.
#
# Updated 2026-06-18 (feat/branch-primitive-sites-draft, BP-2 / pocketpaw#1345):
# sites publish/preview/status are now branch-aware over the BP-1 versions spine,
# fixing the "Live badge lies" bug — a site was stamped ``deployed`` the instant a
# Site doc existed, so a never-deployed / draft pocket still read published+live
# and the builder preview pointed at the live URL instead of the working copy.
#   * publish() — on a successful build, PROMOTES the pocket's current draft
#     version to ``published`` via versions.publish() BEFORE deploy (writing a
#     draft snapshot first if none exists, so a published pointer always lands).
#     ``deployed``/Live still flips true ONLY after the deploy succeeds (the
#     smoke gate already runs before deploy). If deploy fails the Site doc is not
#     persisted (not live) — the published version tag may stand (published !=
#     live). TODO(BP-3): a merge gate will replace this DIRECT publish.
#   * preview_pocket() — serves the DRAFT VERSION's content (the unpublished
#     working copy) via versions.get_draft(), so the Preview tab shows what
#     publish WOULD build. Falls back to the pocket's current rippleSpec/source
#     when no draft row exists yet (e.g. a pre-BP-1 pocket, or a svelte pocket
#     whose source map is not versioned in BP-1).
#   * pocket_status() — derives draft/published + is_live from the version
#     pointers AND the real Site deploy state, NOT "a Site doc exists". A
#     published version (or, for backward compat, a deployed Site doc predating
#     BP-1) reads published; a draft newer than the published version sets
#     has_unpublished_changes; is_live requires published AND the Site doc's real
#     ``deployed``. The artifact is the source pocket: scope_type="pocket",
#     scope_id=<pocket_id>.

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.models.site import SiteDomain as _SiteDomainDoc
from pocketpaw_ee.sites.domain import HostnameStatus
from pocketpaw_ee.sites.dto import (
    DomainStatusResponse,
    SitePreviewResponse,
    SiteResponse,
    SiteStatusResponse,
)
from pocketpaw_ee.sites.generator_client import GeneratorClient

logger = logging.getLogger(__name__)

# The control plane reads the Worker bundle adapter-cloudflare emits here.
_WORKER_BUNDLE_REL = ".svelte-kit/cloudflare/_worker.js"

# BP-2: the source pocket is the versionable artifact behind a site. The Branch
# primitive (BP-1) keys every version on (scope_type, scope_id); for a site the
# scope is the pocket it is published from.
_VERSION_SCOPE_TYPE = "pocket"


def _default_bundle_reader(project_dir: str) -> bytes:
    return Path(project_dir, _WORKER_BUNDLE_REL).read_bytes()


def _capture_base() -> str:
    import os

    return os.environ.get("PAW_CAPTURE_API_BASE", "http://localhost:8888/api/v1")


def _builder_origin() -> str:
    """The dashboard/builder origin an editable Paw Site postMessages its
    section rects to (SE-2b). The generated edit-bridge only accepts messages
    from this exact origin. Defaults to the local dashboard; overridable via
    PAW_SITES_BUILDER_ORIGIN. Used by ``make_site_editable`` when the caller does
    not pass an explicit origin."""
    import os

    return os.environ.get("PAW_SITES_BUILDER_ORIGIN", "http://localhost:8888")


# The default logical form type. The generated /api/submit endpoint sends this
# constant as ``form_type`` (the static page wraps the whole spec in one form, so
# there is no per-form id at submit time), so the seeded mapping must key on it.
_DEFAULT_FORM_TYPE = "lead"

# Default event mapping seeded at publish so a basic contact lead lands with NO
# manual Mongo edit. Maps the common lead fields a marketing form collects; the
# interpolator drops any ``{{ payload.X }}`` whose key is absent from the
# submission (resolves to None), so a form that only sends {full_name, phone}
# still produces a Lead — the extra fields simply come back empty.
_DEFAULT_EVENT_MAPPING: dict[str, Any] = {
    _DEFAULT_FORM_TYPE: {
        "creates": "Lead",
        "fields": {
            "full_name": "{{ payload.full_name }}",
            "phone": "{{ payload.phone }}",
            "email": "{{ payload.email }}",
            "message": "{{ payload.message }}",
        },
    }
}


def _default_allowed_origins() -> list[str]:
    """Origins a freshly published site may capture from before any custom domain
    is connected. ``origin_allowed`` does a host-only match and fails closed on an
    empty list, so we seed the local dev hosts here so the LOCAL smoke (the
    generated site served on localhost) lands a lead with no manual edit. Custom
    production domains are appended by ``add_domain`` when the freelancer connects
    one. Overridable via PAW_SITES_DEFAULT_ORIGINS (comma-separated hosts)."""
    import os

    raw = os.environ.get("PAW_SITES_DEFAULT_ORIGINS", "localhost,127.0.0.1")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _cf_client():
    """Build the real Cloudflare client from settings (env). Injected in tests."""
    import os

    from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

    return CloudflareClient(
        account_id=os.environ["PAW_CF_ACCOUNT_ID"],
        api_token=os.environ["PAW_CF_API_TOKEN"],
        zone_id=os.environ["PAW_CF_ZONE_ID"],
        dispatch_namespace=os.environ.get("PAW_CF_DISPATCH_NAMESPACE", "paw-sites"),
    )


def _local_mode() -> bool:
    """Whether publish() takes the LOCAL deploy branch (skip Cloudflare, serve the
    static site from localhost). True when PAW_SITES_LOCAL=1 is set explicitly, or
    when no Cloudflare account id is configured (a fresh dev box). The real CF path
    runs whenever creds are present — local mode is the fallback, not the default in
    a configured environment."""
    import os

    if os.environ.get("PAW_SITES_LOCAL") == "1":
        return True
    return not os.environ.get("PAW_CF_ACCOUNT_ID")


async def _promote_pocket_draft_to_published(
    *, pocket_id: str, workspace_id: str, author: str | None, content: dict[str, Any]
) -> None:
    """Promote a pocket's current draft version to ``published`` (BP-2).

    Called from ``publish`` after the build succeeds and BEFORE deploy: the
    published version pointer is the durable "this is the version that was
    published" record, independent of whether the deploy itself lands (published
    != live). Reads the current draft via the BP-1 versions service and flips it
    to published; when no draft row exists yet (a pocket published without ever
    going through ``merge_spec``, or a svelte pocket whose source map BP-1 does
    not version), it first writes a draft snapshot of ``content`` so a published
    pointer always lands.

    Lazy-imports the versions service so the sites entity does not take a hard
    import on the versions package and a fork without it degrades gracefully:
    versioning is an additive history/Branch layer over publish, never a gate on
    it, so a failure here is logged and swallowed — the deploy still proceeds.

    TODO(BP-3): the Instinct merge gate will replace this DIRECT promote — a
    publish will branch the draft for human review and the merge accept (not this
    call) will move the published pointer.
    """
    try:
        from pocketpaw_ee.versions import service as versions_service

        draft = await versions_service.get_draft(scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id)
        if draft is None:
            draft = await versions_service.write_draft(
                scope_type=_VERSION_SCOPE_TYPE,
                scope_id=pocket_id,
                workspace_id=workspace_id,
                content=content or {},
                author=author,
            )
        await versions_service.publish(
            scope_type=_VERSION_SCOPE_TYPE,
            scope_id=pocket_id,
            workspace_id=workspace_id,
            version_id=str(draft.id),
        )
    except Exception:  # noqa: BLE001 — versioning must not break publish/deploy
        logger.warning(
            "versions: failed to promote draft→published for pocket %s — "
            "deploy proceeds, published version pointer skipped",
            pocket_id,
            exc_info=True,
        )


def _to_response(doc: _SiteDoc) -> SiteResponse:
    return SiteResponse(
        id=str(doc.id),
        pocket_id=doc.pocket_id,
        name=doc.name,
        script_name=doc.script_name,
        deployed=doc.deployed,
        signed_key=doc.signed_key,
        url=doc.url,
        # SE-2b: surface whether the site is editable (non-empty = carries the
        # edit-bridge) so the UI can show/hide the inline-edit affordance.
        builder_origin=getattr(doc, "builder_origin", ""),
    )


async def publish(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    ripple_spec: dict[str, Any] | None = None,
    theme: dict[str, Any],
    name: str = "",
    engine: str = "ripple",
    source: dict[str, str] | None = None,
    builder_origin: str | None = None,
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Generate, smoke-gate, deploy, and persist a site. Raises SmokeGateFailed
    (from generator_client) if the workerd smoke render fails — the site is not
    deployed and not persisted as deployed.

    Deploy has two branches:
      * REAL Cloudflare (default when creds are present, or when a CF client is
        injected): PUT the Worker bundle into the dispatch namespace.
      * LOCAL fake-deploy (no CF creds / PAW_SITES_LOCAL=1, and no injected CF
        client): persist the built static site and serve it from localhost,
        storing that URL on the Site so the response is openable. Cloudflare is
        not contacted at all.

    ``name`` defaults to the source pocket's own display name when the caller
    omits it (the publish schema promises this). The fallback reads the pocket
    through the pockets service's PUBLIC ``get`` (a wire dict — no Beanie import,
    respecting entity isolation) and uses its ``name`` field; only when the pocket
    has no name does it fall back to "Untitled site". Callers that pre-resolve the
    name (e.g. ``publish_pocket``, which already holds the wire dict) pass it in,
    so the common path does not re-fetch.

    ``builder_origin`` (SE-2b) makes the site EDITABLE: when set, it rides
    ``siteConfig.builderOrigin`` so the paw-sites generator injects the gated
    edit-bridge, and it is persisted on the Site doc so a later component-edit
    republish can re-apply it. ``None`` (the default) publishes a normal,
    non-editable site (empty ``builder_origin`` on the doc, no bridge)."""
    generator = _generator or GeneratorClient()

    # Default a blank name to the source pocket's own display name so the schema's
    # "defaults to the pocket's own name" promise is true at the source-of-truth
    # layer. Cross-entity read goes through the pockets service's PUBLIC function
    # (wire dict), never the Pocket Beanie model.
    site_name = name.strip() if name else ""
    if not site_name:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(pocket_id, user_id)
        site_name = (pocket.get("name") or "").strip()
    if not site_name:
        site_name = "Untitled site"

    site_id = str(ObjectId())
    signed_key = f"site_key_{secrets.token_urlsafe(24)}"

    build = await generator.build(
        ripple_spec=ripple_spec,
        theme=theme,
        site_id=site_id,
        title=site_name,
        capture_api_base=_capture_base(),
        capture_signed_key=signed_key,
        engine=engine,
        source=source,
        builder_origin=builder_origin,
    )

    # BP-2 / #1345: promote the pocket's current draft version to ``published``
    # BEFORE deploy. The build (which runs the smoke gate) has succeeded, so the
    # version being published is known-good; the published pointer is the durable
    # "this is what was published" record even if the deploy below fails
    # (published != live — a failed deploy just leaves the Site doc un-persisted,
    # so the pocket reads not-live while the published tag stands). The snapshot
    # for the promote is the engine's content: the rippleSpec for a ripple site,
    # the {path: contents} source map for a svelte site.
    version_content: dict[str, Any] = (source if engine == "svelte" else ripple_spec) or {}
    await _promote_pocket_draft_to_published(
        pocket_id=pocket_id,
        workspace_id=workspace_id,
        author=user_id,
        content=version_content,
    )

    # Local mode only when the caller did NOT inject a CF client (tests inject a
    # fake CF and expect the real branch) AND the environment selects it.
    use_local = _cloudflare is None and _local_mode()
    url = ""
    if use_local:
        from pocketpaw_ee.sites import local_server

        deploy = _local_deploy or local_server.deploy_local
        url = deploy(site_id, build.project_dir)
    else:
        cf = _cloudflare or _cf_client()
        bundle = _bundle_reader(build.project_dir)
        await cf.put_worker(script_name=site_id, bundle=bundle)

    doc = _SiteDoc(
        id=ObjectId(site_id),
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner=user_id,
        name=site_name,
        script_name=site_id,
        deployed=True,
        signed_key=signed_key,
        url=url,
        # SE-2b: persist the builder origin (or "") so a component-edit republish
        # can re-apply it and the site stays editable across edits.
        builder_origin=builder_origin or "",
        # Seed capture config so a lead lands with no manual Mongo edit: a default
        # mapping keyed on the form_type the generated endpoint sends, and the
        # local dev origins so the local smoke works. add_domain() appends the
        # production hostname when a custom domain is connected.
        allowed_origins=_default_allowed_origins(),
        event_mapping=_DEFAULT_EVENT_MAPPING,
    )
    await doc.insert()
    return doc


async def _load(workspace_id: str, site_id: str) -> _SiteDoc:
    # Guard the cast: a malformed site_id is not a 500. bson raises InvalidId
    # (TypeError for non-str/bytes inputs); both mean "no such site".
    try:
        oid = ObjectId(site_id)
    except (InvalidId, TypeError):
        raise NotFound("site", site_id)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("site", site_id)
    return doc


async def add_domain(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> DomainStatusResponse:
    """Register a custom hostname with Cloudflare for SaaS and store it on the
    site. Returns the ONE CNAME the client pastes at their registrar."""
    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)
    ch = await cf.create_custom_hostname(hostname)
    site.domains.append(
        _SiteDomainDoc(
            hostname=ch.hostname,
            cf_hostname_id=ch.id,
            cname_target=ch.cname_target,
            status=ch.status.value,
        )
    )
    # Authorize the site's own origin for capture: the deployed form posts from
    # this host, and origin_allowed host-matches it against allowed_origins. Done
    # here so connecting a domain needs no separate "allow this origin" step.
    if ch.hostname not in site.allowed_origins:
        site.allowed_origins.append(ch.hostname)
    await site.save()
    return DomainStatusResponse(
        hostname=ch.hostname, cname_target=ch.cname_target, status=ch.status.value
    )


async def domain_status(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> DomainStatusResponse:
    """Poll Cloudflare for the hostname's current status and persist it."""
    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)
    dom = next((d for d in site.domains if d.hostname == hostname), None)
    if dom is None:
        raise NotFound("domain", hostname)
    status: HostnameStatus = await cf.get_hostname_status(dom.cf_hostname_id)
    dom.status = status.value
    await site.save()
    return DomainStatusResponse(
        hostname=dom.hostname, cname_target=dom.cname_target, status=status.value
    )


async def list_domains(*, workspace_id: str, site_id: str) -> list[DomainStatusResponse]:
    """Return the site's custom domains with their current statuses, read from
    the Site doc's ``domains`` list. Tenant-scoped via ``_load`` (a missing /
    cross-tenant site raises NotFound → 404). Backs the Domains tab's reload
    rehydration: no Cloudflare call, just the persisted state."""
    site = await _load(workspace_id, site_id)
    return [
        DomainStatusResponse(hostname=d.hostname, cname_target=d.cname_target, status=d.status)
        for d in site.domains
    ]


async def publish_pocket(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    name: str = "",
    builder_origin: str | None = None,
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Publish a pocket as a site by id — the shared path for the REST router
    and the in-process MCP tool.

    Reads the pocket's rippleSpec + theme via the pockets service (the source of
    truth, which returns the resolved wire dict and raises NotFound / Forbidden
    itself — it never returns None), then delegates to ``publish``. Both the
    ``POST /sites/publish`` endpoint and the ``sites_manager__publish`` MCP tool
    call this so the two surfaces share one code path: a single place reads the
    pocket, derives the theme, and names the site. ``name`` falls back to the
    pocket's own name when the caller does not override it — resolved HERE from the
    wire dict this function already holds, so ``publish`` does not re-fetch the
    pocket on this path. (``publish`` carries the same fallback as a safety net for
    direct callers who pass a blank name.)

    ``builder_origin`` (SE-2b) is forwarded straight through so a publish via this
    shared path can request an editable site (the edit-bridge gates on it). It
    defaults to ``None`` (a normal, non-editable publish).

    The generator / Cloudflare / bundle-reader / local-deploy seams are forwarded
    straight through to ``publish`` so the shared path is unit-testable without
    Bun / workerd / Cloudflare (the same injection contract ``publish`` exposes).
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    # Paw Sites "Svelte track" — the pocket carries which generation engine it
    # was authored on and, for svelte sites, the hand-written source map. The
    # wire dict from the pockets service exposes both (``engine`` defaults to
    # "ripple", ``source`` to None). Forwarded so ``publish`` → ``build`` forks
    # STAGE 2 on the engine (design spec §4.2): svelte materializes ``source``
    # instead of compiling ``rippleSpec``.
    engine = pocket.get("engine") or "ripple"
    source = pocket.get("source") if isinstance(pocket.get("source"), dict) else None

    return await publish(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        ripple_spec=ripple_spec,
        theme=theme,
        engine=engine,
        source=source,
        name=name or pocket.get("name", ""),
        builder_origin=builder_origin,
        _generator=_generator,
        _cloudflare=_cloudflare,
        _bundle_reader=_bundle_reader,
        _local_deploy=_local_deploy,
    )


async def edit_svelte_component(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    component_path: str,
    new_source: str,
    name: str = "",
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Rewrite ONE component of a svelte Paw Site pocket and safely republish.

    The chat-agent entry point for a targeted component edit: it replaces the
    file at ``component_path`` in the pocket's svelte ``source`` map with
    ``new_source`` and republishes the site. The Pocket write is owned by the
    pockets service (``set_svelte_source_file`` — entity isolation); this
    function only orchestrates persist → republish.

    Safety contract — a broken edit must leave NEITHER a broken deploy NOR stale
    source on the pocket:
      1. persist the new component source (the pockets service validates the
         pocket is a svelte site and that ``component_path`` exists, raising
         ValidationError / NotFound otherwise — propagated to the caller);
      2. republish via ``publish_pocket`` (regenerate + smoke-gate + redeploy);
      3. if the republish raises ``SmokeGateFailed`` (the workerd smoke render
         rejects the edited site), ROLL BACK the persisted source to its prior
         contents and re-raise. ``publish`` smoke-gates BEFORE it deploys, so the
         prior live deploy is already untouched; the rollback keeps the stored
         source matching that last-good deploy so a later publish is not broken.

    SE-2b: the republish recovers the ``builder_origin`` stored on the pocket's
    current Site doc and re-applies it, so an EDITABLE site stays editable after
    an edit (and a non-editable one stays non-editable — there is no origin to
    re-apply). Without this, a republish would publish a fresh non-editable site
    and strip the edit-bridge SE-1 gates on ``builderOrigin``.

    The generator / Cloudflare / bundle-reader / local-deploy seams forward
    straight to ``publish_pocket`` so the path is unit-testable without
    Bun / workerd / Cloudflare.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites.generator_client import SmokeGateFailed

    # SE-2b: recover the builder origin the site is currently published with so
    # the republish keeps the edit-bridge. "" (or no prior site) republishes
    # non-editable, exactly as before.
    prior = await _latest_site_for_pocket(workspace_id, pocket_id)
    builder_origin = prior.builder_origin if prior else ""

    # 1. Persist the edit (pockets service owns the Pocket write + validation).
    #    ``previous_source`` is the file's prior contents, held for rollback.
    _wire, previous_source = await pockets_service.set_svelte_source_file(
        pocket_id,
        user_id,
        component_path=component_path,
        new_source=new_source,
    )

    # 2. Republish. 3. On a smoke-gate failure, restore the prior source so the
    #    pocket never carries a component the renderer rejects — then re-raise so
    #    the caller surfaces the reason. The prior deploy is untouched because the
    #    gate fires before publish deploys.
    try:
        return await publish_pocket(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            name=name,
            builder_origin=builder_origin or None,
            _generator=_generator,
            _cloudflare=_cloudflare,
            _bundle_reader=_bundle_reader,
            _local_deploy=_local_deploy,
        )
    except SmokeGateFailed:
        await pockets_service.set_svelte_source_file(
            pocket_id,
            user_id,
            component_path=component_path,
            new_source=previous_source,
        )
        raise


async def _latest_site_for_pocket(workspace_id: str, pocket_id: str) -> _SiteDoc | None:
    """Return the most recently published Site doc for ``pocket_id`` in this
    workspace, or ``None`` if the pocket was never published.

    ``publish`` inserts a fresh Site doc per publish, so a pocket can have more
    than one Site row; the newest (by ``createdAt``) is the live one. SE-2b uses
    this to recover the ``builder_origin`` a republish must re-apply. Tenant-
    scoped on ``workspace``."""
    return await (
        _SiteDoc.find({"workspace": workspace_id, "pocket_id": pocket_id})
        .sort(-_SiteDoc.createdAt)  # type: ignore[operator]
        .first_or_none()
    )


async def make_site_editable(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    builder_origin: str | None = None,
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Republish a pocket's site as EDITABLE (SE-2b).

    Backs ``POST /sites/by-pocket/{pocket_id}/editable``: it republishes the
    pocket with ``builder_origin`` set so the generated page carries the gated
    edit-bridge, and persists that origin on the Site doc. ``builder_origin``
    defaults to the configured dashboard origin (``PAW_SITES_BUILDER_ORIGIN``)
    when the caller does not pass one, so the endpoint works with no body.

    Delegates to ``publish_pocket`` (reads the pocket, regenerates, smoke-gates,
    redeploys), so it inherits the same NotFound / Forbidden propagation and the
    smoke gate — a build that fails the gate raises ``SmokeGateFailed`` and the
    prior deploy is untouched.
    """
    origin = (builder_origin or "").strip() or _builder_origin()
    return await publish_pocket(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        builder_origin=origin,
        _generator=_generator,
        _cloudflare=_cloudflare,
        _bundle_reader=_bundle_reader,
        _local_deploy=_local_deploy,
    )


async def preview_pocket(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
) -> SitePreviewResponse:
    """Read a pocket's DRAFT-version content for the in-app builder Preview tab.

    Loads the source pocket through the pockets service's PUBLIC ``get`` (the wire
    dict — no Beanie import, respecting entity isolation; it raises NotFound /
    Forbidden itself when the pocket is missing or access-denied, which the router
    maps to 404 / 403) to resolve the engine + a current-content fallback.

    BP-2 / #1345: the preview serves the DRAFT VERSION's content (the unpublished
    working copy from the BP-1 versions spine) so the Preview tab shows what
    publish WOULD build — not the live/published URL. It reads
    ``versions.get_draft(scope_type="pocket", scope_id=pocket_id)`` and returns
    that snapshot. It falls back to the pocket's CURRENT content when there is no
    draft row yet (a pre-BP-1 pocket, or a svelte pocket whose source map BP-1
    does not version) so the preview is never empty when content exists:
      * ``engine="ripple"`` (the default) → ``content`` is the rippleSpec.
      * ``engine="svelte"`` → ``content`` is the {path: contents} source map.
    ``content`` is None when the pocket carries nothing to render on that track.

    ``workspace_id`` is unused for the pocket read itself (the pockets service
    scopes on ``user_id``), but it is required on every Sites service function so
    the surface stays uniform and tenant-aware as the read paths converge.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    engine = pocket.get("engine") or "ripple"

    # The pocket's CURRENT content for the engine — the fallback when the Branch
    # primitive has no draft row for this pocket yet.
    if engine == "svelte":
        source = pocket.get("source")
        current = source if isinstance(source, dict) else None
    else:
        ripple_spec = pocket.get("rippleSpec")
        current = ripple_spec if isinstance(ripple_spec, dict) else None

    # Prefer the DRAFT version's snapshot (the working copy publish would build).
    # Versioning is an additive layer — a missing module / read failure must not
    # break the preview, so degrade to the current content on any error.
    draft_content: dict[str, Any] | None = None
    try:
        from pocketpaw_ee.versions import service as versions_service

        draft = await versions_service.get_draft(scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id)
        if draft is not None:
            draft_content = draft.content
    except Exception:  # noqa: BLE001 — versions read is best-effort
        logger.warning(
            "versions: failed to read draft for pocket %s preview — "
            "falling back to current content",
            pocket_id,
            exc_info=True,
        )

    content = draft_content if draft_content is not None else current
    return SitePreviewResponse(pocket_id=pocket_id, engine=engine, content=content)


async def pocket_status(*, workspace_id: str, pocket_id: str) -> SiteStatusResponse:
    """Derive a pocket's draft/published + is_live state from the BRANCH version
    pointers AND the real Site deploy state — NOT from "a Site doc exists".

    BP-2 / #1345 fixes the "Live badge lies" bug: before, a Site doc was enough to
    read published+live, but a Site was stamped ``deployed`` the instant it was
    created, so a never-deployed / draft pocket reported live and the preview
    pointed at a dead URL. Now:

      * ``status`` is "published" when a published version pointer exists
        (``versions.get_published(scope_type="pocket", scope_id=pocket_id)``).
        Backward compat: a Site doc that was deployed BEFORE BP-1 (so it has no
        version rows) still reads "published" — the deployed Site is itself the
        evidence a publish happened. With neither, the pocket reads "draft".
      * ``has_unpublished_changes`` is True when a draft version is NEWER than the
        published one (or a draft exists with nothing published yet) — the edits a
        publish would ship.
      * ``is_live`` is the ONLY signal that earns a "Live" badge: it requires the
        pocket to be published AND a real successful deploy, read from the Site
        doc's ``deployed`` flag (publish only persists the Site doc, with
        ``deployed=True``, AFTER the deploy succeeds — never optimistically). No
        published version + a deployed Site (the legacy case) is still live.
      * ``site_id`` carries the deployed Site's id when one exists.

    Tenant-scoped on ``workspace`` for the Site read (the compound index serves
    it). No Cloudflare call — just persisted state. Versioning is an additive
    layer, so a versions read failure degrades to the Site-doc signal rather than
    breaking the status read.
    """
    doc = await _SiteDoc.find_one({"workspace": workspace_id, "pocket_id": pocket_id})

    published_no: int | None = None
    draft_no: int | None = None
    try:
        from pocketpaw_ee.versions import service as versions_service

        # The BP-1 pointer reads key only on (scope_type, scope_id) — they are
        # artifact-generic and do NOT take workspace_id. A pocket_id is globally
        # unique and belongs to one workspace, so a row's stored ``workspace_id``
        # is the owner's; we ignore any pointer whose workspace does not match
        # this caller's so a foreign workspace cannot read another tenant's
        # published/draft state through a known pocket id (tenant isolation, the
        # same guarantee the workspace-scoped Site read gives).
        published = await versions_service.get_published(
            scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id
        )
        draft = await versions_service.get_draft(scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id)
        if published is not None and published.workspace_id == workspace_id:
            published_no = published.version_no
        if draft is not None and draft.workspace_id == workspace_id:
            draft_no = draft.version_no
    except Exception:  # noqa: BLE001 — versions read is best-effort
        logger.warning(
            "versions: failed to read pointers for pocket %s status — "
            "falling back to the Site-doc signal",
            pocket_id,
            exc_info=True,
        )

    # Published when a published version pointer exists, OR (backward compat) a
    # Site doc was already deployed before BP-1 ever recorded a version.
    has_published = published_no is not None or (doc is not None and doc.deployed)
    status = "published" if has_published else "draft"

    # Unpublished edits: a draft strictly newer than the published version, or a
    # draft with nothing published yet.
    has_unpublished_changes = draft_no is not None and (
        published_no is None or draft_no > published_no
    )

    # Live requires published AND a real successful deploy (the Site doc's
    # ``deployed``). A draft-only pocket, or a published pocket whose deploy
    # failed (no Site doc), is not live.
    deployed = bool(doc is not None and doc.deployed)
    is_live = has_published and deployed

    return SiteStatusResponse(
        pocket_id=pocket_id,
        status=status,
        is_live=is_live,
        has_unpublished_changes=has_unpublished_changes,
        site_id=str(doc.id) if doc is not None else None,
    )


async def list_for_workspace(workspace_id: str) -> list[SiteResponse]:
    cursor = _SiteDoc.find({"workspace": workspace_id}).sort(-_SiteDoc.createdAt)  # type: ignore[operator]
    return [_to_response(doc) async for doc in cursor]


async def site_pocket_ids(workspace_id: str) -> set[str]:
    """Return the set of ``pocket_id``s that have a published Site in this
    workspace.

    Lets the /pockets gallery hide pockets that have been published as a Site
    (they show under /sites instead) WITHOUT the pockets service importing the
    Site Beanie model — the Site read stays in this service, which is the sole
    owner of Site reads (entity isolation). Tenant-scoped on ``workspace``.
    """
    cursor = _SiteDoc.find({"workspace": workspace_id})
    return {doc.pocket_id async for doc in cursor}


async def reserve_local_sites(workspace_id: str | None = None) -> int:
    """Re-serve locally-deployed Paw Sites after a backend restart.

    In LOCAL deploy mode (Phase 3) a published site is served from a per-process
    static server on an EPHEMERAL OS-assigned port that is only started inside
    ``publish``. The built files survive a restart under
    ``local_server.sites_home()/<site_id>/``, but the server does not — so every
    stored ``Site.url`` (``http://127.0.0.1:<old-port>/<site_id>/``) is dead and
    re-publishing one site starts a server on a NEW port, leaving the rest stale.

    This (re)starts the shared static server via ``local_server.ensure_server()``
    and, for each deployed site whose files exist on disk, rewrites the stored
    ``url`` to ``f"{base}/{site_id}/"`` against the now-live base, then saves.
    Returns the count of sites reconciled.

    Scope: ``workspace_id is None`` reconciles ALL workspaces' sites (the boot
    hook path — a restart re-serves everything); a non-None id is tenant-scoped
    to that workspace (the manual POST /sites/reserve path).

    No-op outside local mode: the real Cloudflare path owns its own URLs, so when
    ``_local_mode()`` is False this returns 0 without starting a server. Sites
    with no persisted dir are skipped (nothing to serve)."""
    if not _local_mode():
        return 0

    from pocketpaw_ee.sites import local_server

    base = local_server.ensure_server()
    home = local_server.sites_home()

    # workspace=None → reconcile every tenant's sites (boot hook). A non-None id
    # tenant-filters the read. Both paths only touch deployed sites.
    query: dict[str, Any] = {"deployed": True}
    if workspace_id is not None:
        query["workspace"] = workspace_id

    reconciled = 0
    cursor = _SiteDoc.find(query)
    async for doc in cursor:
        site_id = str(doc.id)
        # Skip sites whose built files are gone — there is nothing to serve, so
        # rewriting the url to a 404-ing path would be worse than leaving it.
        if not (home / site_id).is_dir():
            continue
        fresh_url = f"{base}/{site_id}/"
        if doc.url != fresh_url:
            doc.url = fresh_url
            await doc.save()  # no-event: local-mode URL reconciliation, not a domain mutation
        reconciled += 1
    return reconciled


__all__ = [
    "publish",
    "publish_pocket",
    "preview_pocket",
    "pocket_status",
    "edit_svelte_component",
    "make_site_editable",
    "add_domain",
    "domain_status",
    "list_domains",
    "list_for_workspace",
    "site_pocket_ids",
    "reserve_local_sites",
]
