# ee/pocketpaw_ee/sites/service.py — Sites control-plane orchestration. Sole
# owner of Site writes.
#
# Updated 2026-06-06 (feat/1345-draft-published — draft/published state machine,
# pocketpaw#1345): stop "Live" from lying. New flow on top of the versions module
# (ee/pocketpaw_ee/cloud/versions):
#   * create_draft_site() records the content as the first DRAFT version and
#     persists a Site doc that is NOT deployed and NOT live (status="draft",
#     deployed=False) — creating a site no longer claims it's live.
#   * record_site_draft() is the refine buffer: it writes a NEW draft version and
#     flips the site to "draft", leaving the published version + live deploy
#     untouched until the next publish.
#   * publish() now builds + deploys FIRST, and only on a SUCCESSFUL deploy
#     promotes the draft → published and marks the site deployed/live. A failed
#     build (SmokeGateFailed) or failed Worker upload leaves a recoverable draft
#     and never a phantom-live site. It reuses the draft site's row/identity per
#     pocket instead of stacking a second Site, and records a draft from the
#     passed content if none exists (the direct-publish path).
#   * site_status() returns the draft/published + is_live badge state (works
#     before the first publish); preview()/preview_content() return the current
#     DRAFT content the builder renders (fixes the dead-published-URL iframe).
# _to_response now carries status + is_live. The deploy seam is unchanged: publish
# still calls generator.build(...) and cf.put_worker(...)/local deploy as-is.
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

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.models.site import SiteDomain as _SiteDomainDoc
from pocketpaw_ee.cloud.versions import service as versions_service
from pocketpaw_ee.sites.domain import HostnameStatus
from pocketpaw_ee.sites.dto import (
    DomainStatusResponse,
    PreviewResponse,
    SiteResponse,
    SiteStatusResponse,
)
from pocketpaw_ee.sites.generator_client import GeneratorClient

# The control plane reads the Worker bundle adapter-cloudflare emits here.
_WORKER_BUNDLE_REL = ".svelte-kit/cloudflare/_worker.js"


def _default_bundle_reader(project_dir: str) -> bytes:
    return Path(project_dir, _WORKER_BUNDLE_REL).read_bytes()


def _capture_base() -> str:
    import os

    return os.environ.get("PAW_CAPTURE_API_BASE", "http://localhost:8888/api/v1")


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


def _to_response(doc: _SiteDoc) -> SiteResponse:
    # ``status`` is the denormalized version state on the doc ("draft" |
    # "published"); ``is_live`` is the deploy-confirmed axis (the site was
    # actually deployed). A draft site reads status="draft", is_live=False.
    return SiteResponse(
        id=str(doc.id),
        pocket_id=doc.pocket_id,
        name=doc.name,
        script_name=doc.script_name,
        deployed=doc.deployed,
        signed_key=doc.signed_key,
        url=doc.url,
        status=getattr(doc, "status", "draft"),
        is_live=bool(doc.deployed),
    )


def _version_content(
    *, engine: str, ripple_spec: dict[str, Any] | None, source: dict[str, str] | None
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Split the publish payload into the (content, source) snapshot pair the
    versions log stores. Svelte sites version their ``source`` map; ripple sites
    version their ``rippleSpec``. The other field is None on each track."""
    if engine == "svelte":
        return None, source
    return ripple_spec, None


async def _find_site_for_pocket(workspace_id: str, pocket_id: str) -> _SiteDoc | None:
    """The Site row (if any) for a pocket in a workspace. A pocket has at most one
    site (the compound (workspace, pocket_id) index), so publish reuses it rather
    than stacking a second row when a draft was created first."""
    return await _SiteDoc.find_one(
        {"workspace": workspace_id, "pocket_id": pocket_id}
    )


async def _resolve_site_name(
    *, name: str, pocket_id: str, user_id: str
) -> str:
    """Default a blank name to the source pocket's own display name (the publish
    schema promises this), falling back to "Untitled site". Reads the pocket via
    the pockets service's PUBLIC ``get`` (a wire dict — no Beanie import)."""
    site_name = name.strip() if name else ""
    if not site_name:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(pocket_id, user_id)
        site_name = (pocket.get("name") or "").strip()
    if not site_name:
        site_name = "Untitled site"
    return site_name


async def create_draft_site(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    ripple_spec: dict[str, Any] | None = None,
    theme: dict[str, Any],
    name: str = "",
    engine: str = "ripple",
    source: dict[str, str] | None = None,
) -> _SiteDoc:
    """Create a site as a DRAFT — record its content as the first draft version
    and persist a Site doc that is NOT deployed and NOT live (status="draft",
    deployed=False). This is the fix for "a site is stamped deployed the moment
    it's created": creating a site no longer claims it's live. An explicit
    ``publish`` builds, deploys, and flips it live.

    The site id + signed key + script name are minted here (they're identifiers,
    not deploy state) so the draft has a stable identity the later publish reuses.
    Idempotent per pocket: if a site already exists for this pocket, the new
    content is recorded as a fresh draft and the existing doc is returned
    (status reset to "draft" — there are now unpublished edits)."""
    content, src = _version_content(engine=engine, ripple_spec=ripple_spec, source=source)
    await versions_service.record_draft(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        content=content,
        source=src,
        engine=engine,
        author=user_id,
        origin="create",
    )
    site_name = await _resolve_site_name(name=name, pocket_id=pocket_id, user_id=user_id)

    existing = await _find_site_for_pocket(workspace_id, pocket_id)
    if existing is not None:
        existing.name = site_name
        existing.status = "draft"  # new unpublished edits
        await existing.save()
        return existing

    site_id = str(ObjectId())
    signed_key = f"site_key_{secrets.token_urlsafe(24)}"
    doc = _SiteDoc(
        id=ObjectId(site_id),
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner=user_id,
        name=site_name,
        script_name=site_id,
        deployed=False,
        status="draft",
        signed_key=signed_key,
        url="",
        allowed_origins=_default_allowed_origins(),
        event_mapping=_DEFAULT_EVENT_MAPPING,
    )
    await doc.insert()
    return doc


async def record_site_draft(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    content: dict[str, Any] | None = None,
    source: dict[str, str] | None = None,
    engine: str = "ripple",
) -> _SiteDoc | None:
    """Refine a site's content: write a NEW draft version and mark the site
    "draft" (there are unpublished edits). The published version's content — and
    the live deploy — are untouched until the next publish. This is the buffer the
    bug report asked for ("every refine overwrites the live thing with no draft
    buffer and no way back"). Returns the Site doc if one exists yet (a site can
    be refined before it is first published), else None."""
    await versions_service.record_draft(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        content=content,
        source=source,
        engine=engine,
        author=user_id,
        origin="refine",
    )
    doc = await _find_site_for_pocket(workspace_id, pocket_id)
    if doc is not None:
        doc.status = "draft"
        await doc.save()
    return doc


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
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Promote the pocket's draft to published, build, smoke-gate, deploy, and
    persist the site as live. Raises SmokeGateFailed (from generator_client) if
    the workerd smoke render fails, or the deploy error if the Worker upload
    fails — in BOTH cases the version stays a draft and the site is NOT marked
    live (the "Live only after a successful deploy" guarantee).

    Order matters: build + deploy run FIRST; only on success does the draft
    version get promoted to published and the Site doc persisted as
    deployed/published. So a failed publish leaves a recoverable draft and never
    a phantom-live site.

    Idempotent per pocket: reuses the Site row created by ``create_draft_site``
    (and its minted id / signed key / script name) when one exists, instead of
    stacking a second row; otherwise mints a fresh identity (the direct-publish
    path, e.g. ``publish_pocket``). If no draft version exists yet (direct
    publish), one is recorded from the passed content before promotion, so publish
    always has a version to promote.

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
    so the common path does not re-fetch."""
    generator = _generator or GeneratorClient()

    site_name = await _resolve_site_name(name=name, pocket_id=pocket_id, user_id=user_id)

    # Reuse the draft site's identity if it exists, so publish updates that row
    # rather than creating a second site for the same pocket.
    existing = await _find_site_for_pocket(workspace_id, pocket_id)
    if existing is not None:
        site_id = str(existing.id)
        signed_key = existing.signed_key
    else:
        site_id = str(ObjectId())
        signed_key = f"site_key_{secrets.token_urlsafe(24)}"

    # Ensure there is a draft version to promote. The create-then-publish path
    # already recorded one (the user's working content); the direct-publish path
    # (no prior draft) records one now from the passed content so the published
    # version reflects exactly what was deployed.
    if await versions_service.get_draft(workspace_id=workspace_id, pocket_id=pocket_id) is None:
        content, src = _version_content(
            engine=engine, ripple_spec=ripple_spec, source=source
        )
        await versions_service.record_draft(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            content=content,
            source=src,
            engine=engine,
            author=user_id,
            origin="publish",
        )

    # Build + deploy FIRST. A failure here propagates BEFORE any promotion or
    # live-marking, so the version stays a draft and the site is not live.
    build = await generator.build(
        ripple_spec=ripple_spec,
        theme=theme,
        site_id=site_id,
        title=site_name,
        capture_api_base=_capture_base(),
        capture_signed_key=signed_key,
        engine=engine,
        source=source,
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

    # Deploy succeeded — NOW promote the draft to published and mark the site
    # live. The deploy status is mirrored onto the pocket pointer as "live".
    await versions_service.publish_draft(workspace_id=workspace_id, pocket_id=pocket_id)

    if existing is not None:
        existing.name = site_name
        existing.deployed = True
        existing.status = "published"
        existing.url = url
        await existing.save()
        doc = existing
    else:
        doc = _SiteDoc(
            id=ObjectId(site_id),
            workspace=workspace_id,
            pocket_id=pocket_id,
            owner=user_id,
            name=site_name,
            script_name=site_id,
            deployed=True,
            status="published",
            signed_key=signed_key,
            url=url,
            # Seed capture config so a lead lands with no manual Mongo edit: a
            # default mapping keyed on the form_type the generated endpoint sends,
            # and the local dev origins so the local smoke works. add_domain()
            # appends the production hostname when a custom domain is connected.
            allowed_origins=_default_allowed_origins(),
            event_mapping=_DEFAULT_EVENT_MAPPING,
        )
        await doc.insert()

    # Mirror the real version pointers + the live deploy onto the pocket pointer
    # cache in one write (best-effort — skipped if the pocket doc isn't present).
    vstatus = await versions_service.status_for(
        workspace_id=workspace_id, pocket_id=pocket_id
    )
    await versions_service._sync_pocket_pointers(
        workspace_id,
        pocket_id,
        draft_no=vstatus.draft_version,
        published_no=vstatus.published_version,
        deploy_status="live",
    )
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
        _generator=_generator,
        _cloudflare=_cloudflare,
        _bundle_reader=_bundle_reader,
        _local_deploy=_local_deploy,
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


async def site_status(*, workspace_id: str, pocket_id: str) -> SiteStatusResponse:
    """The draft/published status for a site, by source pocket id — backs the
    Draft/Live badge. ``status`` is the version state (draft = unpublished edits |
    published = the live candidate is the latest version), and ``is_live`` is the
    deploy-confirmed axis (the Site was actually deployed). Works before the first
    publish (no Site doc yet): version state comes from the versions log,
    ``is_live`` is False until a deploy succeeds. ``status`` "none" surfaces as
    "draft" to the badge (a pocket with no versions yet is, practically, a draft).
    """
    vstatus = await versions_service.status_for(
        workspace_id=workspace_id, pocket_id=pocket_id
    )
    site = await _find_site_for_pocket(workspace_id, pocket_id)
    is_live = bool(site.deployed) if site is not None else False
    # Map the version "none" (no versions yet) to the badge's "draft".
    badge_status = "published" if vstatus.status == "published" else "draft"
    return SiteStatusResponse(
        pocket_id=pocket_id,
        site_id=str(site.id) if site is not None else None,
        status=badge_status,
        is_live=is_live,
        draft_version=vstatus.draft_version,
        published_version=vstatus.published_version,
        url=site.url if site is not None else "",
    )


async def preview_content(
    *, workspace_id: str, pocket_id: str
) -> dict[str, Any] | None:
    """The current DRAFT content the builder preview renders — the rippleSpec for
    a ripple site, or the svelte source map for a svelte site. Returns the WORKING
    version (the latest draft), NOT the published URL — this is the fix for the
    builder preview iframing a dead local-serve URL. ``None`` when the pocket has
    no versions yet."""
    return await versions_service.get_draft_content(
        workspace_id=workspace_id, pocket_id=pocket_id
    )


async def preview(*, workspace_id: str, pocket_id: str) -> PreviewResponse:
    """``preview_content`` wrapped in the response DTO (with the engine), for the
    REST preview endpoint."""
    draft = await versions_service.get_draft(
        workspace_id=workspace_id, pocket_id=pocket_id
    )
    if draft is None:
        return PreviewResponse(pocket_id=pocket_id, engine="ripple", content=None)
    content = draft.source if draft.engine == "svelte" else draft.content
    return PreviewResponse(pocket_id=pocket_id, engine=draft.engine, content=content)


__all__ = [
    "publish",
    "publish_pocket",
    "create_draft_site",
    "record_site_draft",
    "site_status",
    "preview",
    "preview_content",
    "add_domain",
    "domain_status",
    "list_domains",
    "list_for_workspace",
    "site_pocket_ids",
]
