# ee/pocketpaw_ee/cloud/models/site.py — a published Paw Site + its custom
# domains. workspace-scoped. The capture config (origin allowlist, signed key,
# rate limits, event mapping) lives here so the public capture endpoint can
# harden ingest without a second store. SiteDomain tracks the Cloudflare-for-
# SaaS hostname lifecycle the Domains panel polls.
#
# Updated 2026-08-12 (sites Settings consolidation): added the owner's CLIENT
# record — ``client_name`` / ``client_contact`` / ``client_notes`` and a
# ``client_invoices`` list of ``SiteInvoice``. TWO BILLING RELATIONSHIPS NOW MEET
# ON THIS DOCUMENT AND THEY ARE NOT THE SAME ONE: ``plan_tier`` /
# ``subscription_status`` are what the site's owner pays US, while these four are
# what the owner's OWN client owes THEM. Only the first is a real charge; the
# second is an address book and a receipt book, and nothing in the deploy or
# billing lanes reads it. It lives on the Site rather than in its own collection
# because it is per-site by definition and has no lifecycle of its own — it is
# born and deleted with the site. All four default empty, so no migration.
#
# Updated 2026-07-31 (provisioning brick): added ``provision_started_at`` — the
# clock behind a BOUNDED single-flight guard. ``provision_status="provisioning"``
# alone is a one-way door: a job that no worker ever consumed, or that died before
# writing a terminal status, pinned the Site there and every later publish of that
# pocket short-circuited to the in-progress no-op. Stamp the entry, and the
# service can re-enqueue once the window lapses. Do NOT reuse ``updated_at`` for
# this — the Site model has no such field, and reading one that isn't there makes
# every row look stale and defeats the single-flight guard entirely.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.2): new Site +
# SiteDomain documents. Capture-hardening fields mirror
# ``pocketpaw.sites_capture.SiteFormConfig`` so the public endpoint reads one
# store. Compound (workspace, pocket_id) index serves the per-pocket lookup.
#
# Updated 2026-06-01 (Phase 3 — local fake-deploy): added ``url`` — the deployed
# site's canonical URL. In LOCAL deploy mode (no Cloudflare creds) publish()
# stores the localhost URL the per-site static server serves
# (http://127.0.0.1:<port>/<site_id>/) so the SiteResponse carries a real
# openable address for the cmux smoke. In the real CF path it is left "" in v1
# (the deployed Worker is reached via its custom domain, surfaced through the
# domains list) until a canonical workers.dev URL is wired.
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): added
# ``builder_origin`` — the builder origin the site was published with, or "" for
# a normal (non-editable) site. When set, the generated page carries the gated
# edit-bridge keyed on it. Persisted so a component-edit republish re-applies it
# and the site stays editable across edits.
#
# Updated 2026-06-18 (feat/sites-stable-identity, PERF-1): a published Paw Site now
# has a STABLE per-(workspace, pocket_id) identity — its ``_id`` is derived
# deterministically from the pair (``service._live_object_id``), so a re-publish
# UPSERTS the SAME Site doc (one canonical row per pocket) instead of inserting a
# fresh one each time. No schema change: the upsert keys on the primary ``_id``
# (already unique), so the existing compound (workspace, pocket_id) index — which
# still serves the per-pocket reads — is sufficient and no new unique key is added.
#
# Updated 2026-06-18 (feat/sites-dedupe-migration, PERF-2): added ``archived`` — a
# non-destructive tombstone flag for the duplicate Site docs the pre-PERF-1 minting
# left behind. PERF-1 made NEW publishes stable (one upserted doc per pocket), but
# EXISTING data still carries dupes (one pocket had 14 docs). The PERF-2 dedupe
# migration (``sites.dedupe``) keeps ONE canonical doc per (workspace, pocket_id)
# active and sets ``archived=True`` on the rest — it NEVER deletes, so the data is
# recoverable. The gallery read (``service.list_for_workspace`` / listSites) filters
# ``archived`` so each pocket shows exactly one card. Defaults False, so every
# existing doc and every fresh publish reads active until the migration archives it.
#
# Updated 2026-06-19 (P2b-backend — "Last Deployed"): added ``deployed_at`` — the UTC
# timestamp of the most recent SUCCESSFUL live deploy. ``service.publish`` stamps it
# (``datetime.now(UTC)``) ONLY when a non-preview deploy succeeds and ``deployed``
# flips True — NOT on a preview/edit/arm build and NOT on every ``updatedAt`` bump,
# so it is a true "last shipped" marker, not a "last touched" one. Defaults None;
# backfill is not required (pre-P2b rows read null, exposed as None on the DTOs).
#
# Updated 2026-06-20 (DS-2 — dynamic-site D1 bindings): added ``d1_database_id`` —
# the Cloudflare D1 database id a DYNAMIC site's deployed Worker is bound to.
# ``service.publish`` derives a STABLE per-(workspace, pocket) id for a
# ``pattern="dynamic"`` publish, persists it here on first publish, and REUSES the
# stored value on every re-publish so the binding target (and the data behind it)
# is stable across deploys. "" for static sites (no D1 binding). Defaults "", so
# pre-DS-2 rows and every static publish read empty — no migration.
#
# Updated 2026-06-24 (integration/billing-credits, BC-9 — per-site annual plan):
# added the per-site billing fields a published site carries on its OWN recurring
# annual plan (the Webflow model — each site has its own tier, not just the
# workspace plan). ``plan_tier`` is the site-plan catalog key (basic | pro |
# business — ``site_plans.SITE_PLAN_CATALOG``), ``subscription_id`` the Dodo
# subscription this site's annual sub maps to, ``annual_renewal_date`` the next
# renewal stamp the webhook updates, and ``subscription_status`` the lifecycle
# (none | active | cancelled). ``service.publish_pocket`` stamps ``plan_tier`` +
# ``subscription_id`` at publish; the per-site ``subscription.*`` webhook (routed
# by a ``site_id`` on its metadata) advances ``subscription_status`` /
# ``annual_renewal_date``. All default to backward-compatible values (None /
# "none") so every pre-BC-9 row and every workspace-plan-only site reads as having
# no per-site sub — no migration.
#
# Updated 2026-06-24 (feat/charge-first-sites — charge-first per-site publishing):
# a PAID-tier site is now created as PENDING and NOT deployed live until the
# ``subscription.active`` webhook confirms payment. Two additions support that:
#   * ``pending_deploy_inputs`` — the deploy inputs (rippleSpec / theme / engine /
#     svelte source / pattern / builder_origin / name) captured at publish time so
#     the webhook-time ``activate_site`` can run the deferred deploy WITHOUT
#     re-reading the pocket (the webhook only carries workspace_id + site_id, and
#     the pocket's draft may have moved on by activation time). Set ONLY for a
#     pending paid publish; "" / empty for a free/live publish, and cleared once
#     the site is activated. Default empty dict so every pre-charge-first row reads
#     as having no pending deploy.
#   * ``_checkout_url`` — a TRANSIENT pydantic PrivateAttr (NOT persisted to Mongo)
#     the publish path stashes the Dodo checkout link on so the router can surface
#     it on ``SiteResponse.checkout_url`` for a paid publish. None for a free
#     publish. Private so it never round-trips through the DB.
#
# Updated 2026-07-08 (DP0-1 — Dynamic Paw Sites Phase 0 provisioning state): added
# ``provision_status`` (none | provisioning | provisioned | failed) alongside
# ``d1_database_id``. It tracks where a dynamic site is in the durable D1 provision
# job. The contract the job upholds: it persists ``d1_database_id`` IMMEDIATELY
# after the D1 is created (status still ``provisioning``) so a retry reuses the same
# D1 instead of orphaning a second one; status advances to ``provisioned`` only
# after migrate + deploy succeed, and to ``failed`` on error. Defaults ``"none"`` so
# every static site and every pre-DP0 row reads "not provisioning" — no migration.
#
# Updated 2026-07-09 (DP0-4 — publish async split + single-flight): added
# ``_provision_job_id`` — a TRANSIENT pydantic PrivateAttr (NOT persisted to Mongo),
# mirroring ``_checkout_url``. A DYNAMIC-site publish no longer deploys inline; it
# ensures the Site doc in ``provision_status="provisioning"`` and enqueues the
# durable ``provision_site`` job, stashing the enqueued job id here so the router
# can surface it on ``SiteResponse.provision_job_id``. None for a static publish and
# for any DB-loaded doc (the PrivateAttr defaults to None). Private so it never
# round-trips through the DB.
#
# Updated 2026-08-10 (SL-2 slice 2 — the build lane got its first caller): pinned the
# FORMAT of ``build_reason`` to ``"<rung>:<cause>"``. It was described as "the rung name,
# plus the cause for a user-blamed failure", which is two shapes and would have had every
# consumer branch on blame before it could read a rung. One shape, both halves from closed
# sets, colon-separated — see the field. The writer is ``sites/build_job.py``; the fields
# themselves are written only through the ``sites.service`` seams, and only with a
# targeted ``set`` so a minutes-long build can never roll back a concurrent publish.
#
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): added ``import_report`` —
# the per-import summary an IMPORTED site carries ({pages, asset_count, asset_bytes,
# forms, scripts, warnings}), persisted by the import service after the html deploy
# and surfaced on ``SiteResponse.import_report``. Derived minimally from the zip
# contents today; the generator-side import plan enriches it (form-rewiring
# verdicts) once the parallel paw-sites slice lands. Defaults to an empty dict, so
# every non-imported site and every pre-SI-4 row reads "no import" — no migration.
#
# Updated 2026-07-14 (Paw Bar concierge seam, T1): the Site's ``signed_key`` +
# ``allowed_origins`` (already here since RFC 12) ARE the public, origin-bound
# embed credential a Paw Bar concierge authenticates with — no parallel key model
# is introduced. Three additive fields make that credential a first-class scoped
# key: ``scopes`` (what a resolved concierge request may do — chat / kb.read /
# event.ingest by default), ``revoked`` (a kill switch the resolver fails closed
# on), and a ``rotate_signed_key`` helper (regenerate the embed key, e.g. after a
# leak — caller persists). A ``signed_key`` index backs the key→Site lookup
# ``auth.site_keys.resolve_site_key`` does on every concierge request. All defaults
# are backward-compatible (existing docs read ``revoked=False`` + the default
# scope set), so no migration.
#
# Updated 2026-07-16 (Paw Bar concierge settings + kill switch, D1 — folds in
# staffed-sites SS-6): added the owner-facing concierge controls, distinct from the
# key-level ``revoked`` flag above. ``concierge_enabled`` is the owner's on/off kill
# switch for the concierge itself (NOT the embed key): the three public paw-bar
# entry points (frame / chat / action) fail closed with a 403 when it is False, so
# an owner can silence the bar without deleting the Site or rotating the key.
# ``revoked`` still cuts the KEY (401, anti-enumeration); ``concierge_enabled=False``
# refuses the resolved concierge (403) — two different switches. ``concierge_greeting``
# is the opening line the glass bar renders; it rides into the frame's
# ``window.__PAWBAR__`` config payload. Both default backward-compatibly
# (``concierge_enabled=True`` + ``concierge_greeting=""``), so every existing Site
# reads as enabled with no greeting — no migration.
#
# Updated 2026-07-26 (concierge transcripts): added ``concierge_store_transcripts``
# — the owner's retention switch for the VISITOR half of a conversation. It is
# deliberately its own toggle rather than a fold into ``concierge_enabled``,
# because it is the one concierge setting that governs whether NEW personal data
# is collected (visitor free text can carry a name, an email, an order number).
# Defaults True so the owner-facing transcript is a real two-sided conversation;
# an owner on a privacy-sensitive site turns it off and keeps the concierge.
#
# Updated 2026-07-26 (site knowledge sync): added ``kb_article_ids`` /
# ``kb_synced_at`` / ``kb_sync_error`` — the bookkeeping behind "this site's own
# pages are in the pocket KB its concierge reads". Without it a dedicated concierge
# was provisioned knowledge-empty and could not answer a question about the business
# it fronts. The ids exist so a re-sync can prune what a renamed page left behind
# without clearing a scope that also holds owner-uploaded files. All default
# empty/None, so no migration.
#
# Updated 2026-08-07 (SC-1 — a site's card shows its own screenshot): added
# ``preview_image_url`` — the stored URL of a screenshot of this site's live page,
# written by the best-effort capture ``sites.screenshot`` schedules from the tail
# of a successful deploy. Empty when no screenshot has landed (never deployed, no
# public url yet, capture failed, or Cloudflare is unconfigured), and the gallery
# card falls back to its text layout on empty — so it is always optional and never
# a gate on publishing. Defaults "" so every existing row reads "no preview" — no
# migration.
#
# Updated 2026-08-07 (SC-3 — the card stops lying after a republish): no schema
# change, only the write POLICY for that field, recorded where the field lives.
# ``preview_image_url`` is rewritten on EVERY successful deploy (a republish
# included — there is no TTL and no "only if empty" guard, since a republish is
# exactly the case where a value exists and is wrong) and by an explicit
# POST /sites/{site_id}/preview-refresh. Every capture stores a NEW uploads row, so
# the value changes each time and nothing overwrites bytes behind a stable URL —
# a reader may treat an unchanged value as unchanged art. Written by targeted
# ``set()``, never ``save()``: the capture lands seconds after the publish that
# scheduled it, holding a doc snapshotted before it.

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed
from pydantic import BaseModel, Field, PrivateAttr

from pocketpaw.paw_bar.appearance import ConciergeAppearance
from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteDomain(BaseModel):
    """A custom hostname attached to a site (Cloudflare for SaaS)."""

    hostname: str
    cf_hostname_id: str = ""
    cname_target: str = ""
    status: str = "pending"  # pending | verifying | live | error
    # Cloudflare Worker route bound to ``<hostname>/*``, which is what decides that
    # THIS site answers this domain. The custom hostname alone only gets Cloudflare to
    # accept the request. Empty means no route was written: either the deploy mode has
    # no per-site Worker to point at (local / WfP), or the row predates the routing
    # lane. Stored rather than re-derived because teardown needs the id, and a route
    # nobody recorded is an orphan nobody can delete.
    cf_route_id: str = ""


class SiteInvoice(BaseModel):
    """One manual receipt the site's OWNER recorded against their own client.

    This is bookkeeping the owner keeps, not a charge we process: nothing here
    moves money, and the sites service never reads it back for billing. Amounts
    are integer MINOR units (cents) so a receipt cannot drift through float
    arithmetic on its way to and from the wire.
    """

    id: str
    issued_at: datetime
    amount_cents: int = 0
    currency: str = "USD"
    paid: bool = True
    note: str = ""


class Site(TimestampedDocument):
    """A published site generated from a pocket's rippleSpec."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    owner: str
    name: str = ""
    # Workers-for-Platforms script name (== site id) once deployed.
    script_name: str = ""
    deployed: bool = False
    # Which target the last SUCCESSFUL deploy actually used: "" (never deployed) |
    # "local" | "workers" | "wfp". Stamped only after a deploy returns, so it records
    # what happened rather than what was configured.
    #
    # Exists because PAW_CF_DEPLOY_MODE cannot answer "does this site have its own
    # route-addressable Worker". It is read at request time, while the Worker was
    # created at deploy time, and the two disagree constantly: `provision_deploy`
    # degrades local -> workers for dynamic sites; nothing ever deletes a Worker, so a
    # site published under `workers` keeps its Worker after the env moves to `wfp`; and
    # a republish resets a dynamic site's provision_status while last deploy's Worker is
    # still live and serving. Each disagreement writes — or fails to write — a custom
    # domain's route against the wrong answer.
    deploy_target: str = ""
    # P2b: UTC timestamp of the most recent SUCCESSFUL live deploy. Stamped by
    # service.publish ONLY when a non-preview deploy succeeds (when ``deployed``
    # flips True) — never on a preview/edit build, never on a plain updatedAt bump.
    # None until the pocket has been deployed at least once (old rows read null).
    deployed_at: datetime | None = None
    # Canonical deployed URL. LOCAL mode: the localhost URL the per-site static
    # server serves. CF mode: "" in v1 (reached via custom domain).
    url: str = ""
    # DS-2: the Cloudflare D1 database id this site's deployed Worker is bound to.
    # Set ONLY for a dynamic site (pattern == "dynamic"); "" for a static site.
    # Stable across re-publishes (publish reuses the stored value) so the D1
    # binding target — and the data behind it — never moves under a live site.
    d1_database_id: str = ""
    # DP0-1: where a dynamic site sits in the durable D1 provision job
    # (none | provisioning | provisioned | failed). Contract: the job persists
    # ``d1_database_id`` IMMEDIATELY after the D1 is created (status still
    # ``provisioning``) so a retry REUSES the same D1 instead of orphaning a second
    # one; status becomes ``provisioned`` only after migrate + deploy succeed, and
    # ``failed`` on error. Defaults "none" for static sites and pre-DP0 rows.
    provision_status: str = "none"
    # When the CURRENT provisioning attempt started (UTC). Exists so the
    # single-flight guard can be bounded: a job that is never consumed or dies
    # without writing a terminal status would otherwise leave the doc
    # ``provisioning`` forever and make every later publish of the pocket a
    # silent no-op — an unpublishable pocket with no error to see. None on
    # static sites and pre-existing rows; a row with no stamp is treated as
    # stale, which is the safe direction (a redundant enqueue costs one
    # idempotent job, a stuck guard costs every future publish).
    provision_started_at: datetime | None = None
    # ── SG-9i: the ephemeral-build lane's own lifecycle ─────────────────────
    # Deliberately SEPARATE from the provision_* trio rather than reusing it. A site
    # is provisioned once (its D1 created and migrated) but REBUILT many times, so
    # collapsing them would make a rebuild look like a re-provision and would let one
    # overwrite the other's status.
    #
    # ``build_status`` — none | queued | building | built | failed.
    # ``queued`` is a FIRST-CLASS state, not cosmetic. Once a concurrency cap exists a
    # publish can wait before it starts, and a queued build is indistinguishable from
    # a hung one unless the wire says so — which turns the cap into support tickets.
    build_status: str = "none"
    # When the CURRENT build attempt entered queued/building (UTC). Same bounded
    # single-flight reasoning as ``provision_started_at``, and the same asymmetry: a
    # row with no stamp reads as STALE, because a redundant enqueue costs one
    # idempotent build while a stuck guard costs the pocket every future publish.
    #
    # Do NOT substitute ``updated_at`` for this. The DP0-4 comment above says the same
    # thing and it is worth repeating where the field is: this model has no such
    # field, so reading one would make every row look stale and silently disable the
    # guard entirely.
    build_started_at: datetime | None = None
    # The build job's id — PERSISTED, unlike ``_provision_job_id`` below, which is a
    # transient PrivateAttr that only exists on the response object that enqueued it.
    # That works for a provision the caller watches synchronously and fails for a
    # build: a queued build is exactly the case where the user reloads the page, and
    # on reload a transient id is gone, so the client loses its polling handle at the
    # precise moment the wait is longest.
    build_job_id: str | None = None
    # SL-2: WHY the build reached ``build_status``.
    #
    # FORMAT, fixed by ``sites/build_job.py`` when the lane got its first caller:
    # ``"<rung>:<cause>"``, both halves from closed sets. The rung is a
    # ``daytona_build.BuildOutcome`` (``completed_ok`` / ``build_failed`` / ``timed_out``
    # / ``infra_lost``) or one of the job's own pre-sandbox rungs (``engine_not_buildable``
    # / ``scaffold_failed`` / ``scaffold_empty`` / ``sandbox_unavailable`` /
    # ``artifact_missing`` / ``enqueue_failed``); the cause is the classifier's own
    # machine-readable ``reason``, e.g. ``build_failed:install_failed`` or
    # ``infra_lost:build_killed_by_signal_137``. ONE shape for every rung rather than
    # "the outcome, plus a cause when the user is to blame", so a consumer parses once and
    # can always split on the colon to group by rung.
    #
    # THIS FIELD IS WHAT MAKES A TERMINAL FAILURE HONEST. Without it every
    # classification the lane computes dies at the boundary: the row can say ``failed``
    # and nothing can say whether the user's code broke or we lost the container. Those
    # two need OPPOSITE handling — one is the user's to fix, the other is ours to retry
    # — so a ``failed`` with no reason is not a smaller error, it is an unactionable
    # one, and the fallback ("your build failed") is exactly the mis-report the whole
    # sentinel design exists to prevent.
    #
    # SAFE TO SURFACE. It carries a fixed rung name, never raw stderr: a build's error
    # text is the user's own code and can contain anything, including a token pasted
    # into a config. The stderr tail stays in logs. Same reasoning as
    # ``jobs/worker.py``'s ``_safe_failure_message``.
    build_reason: str | None = None
    # BC-9: per-site annual plan (the Webflow model — each published site has its
    # OWN recurring annual plan on a tier, distinct from the workspace plan).
    # ``plan_tier`` is the site-plan catalog key (basic | pro | business — see
    # ``billing.site_plans``); None until a publish stamps one. ``subscription_id``
    # is the Dodo subscription id for this site's annual sub (None when Dodo is
    # unconfigured — the tier is recorded without a live charge in v1).
    # ``annual_renewal_date`` is the next renewal stamp the per-site webhook
    # updates; ``subscription_status`` tracks the lifecycle (none | active |
    # cancelled). Defaults keep pre-BC-9 / workspace-plan-only sites at "no sub".
    plan_tier: str | None = None
    subscription_id: str | None = None
    annual_renewal_date: datetime | None = None
    subscription_status: str = "none"
    # charge-first: the deploy inputs captured at publish time for a PENDING paid
    # site, so the ``subscription.active`` webhook can run the deferred deploy
    # without re-reading the pocket (the webhook carries only workspace_id +
    # site_id, and the pocket's draft may have advanced by activation time). Keys:
    # ripple_spec, theme, engine, source, pattern, builder_origin, name. Empty for
    # a free/live publish; cleared once the site is activated.
    pending_deploy_inputs: dict[str, Any] = Field(default_factory=dict)
    # charge-first: TRANSIENT (NOT persisted) Dodo checkout link for a paid
    # publish. The publish path stashes it here so the router can surface it on
    # ``SiteResponse.checkout_url``; a PrivateAttr so it never serializes to Mongo.
    _checkout_url: str | None = PrivateAttr(default=None)
    # DP0-4: TRANSIENT (NOT persisted) id of the durable ``provision_site`` job a
    # DYNAMIC-site publish enqueued. The publish path stashes it here so the router
    # can surface it on ``SiteResponse.provision_job_id``; a PrivateAttr so it never
    # serializes to Mongo. None for a static publish and any DB-loaded doc.
    _provision_job_id: str | None = PrivateAttr(default=None)
    # PERF-2: a non-destructive tombstone for duplicate Site docs the pre-PERF-1
    # per-publish ObjectId minting left behind. The dedupe migration keeps ONE
    # canonical doc per (workspace, pocket_id) active and sets this True on the
    # rest (never deletes). The gallery read filters it so each pocket shows once.
    archived: bool = False
    # SE-2b: the builder origin this site was published with, or "" when it was
    # published as a normal (non-editable) site. When set, the generated page
    # carries the gated edit-bridge keyed on this origin. Persisted so a
    # component-edit republish can re-apply it and the site stays editable.
    builder_origin: str = ""
    # SI-4: the import summary an IMPORTED site carries ({pages, asset_count,
    # asset_bytes, forms, scripts, warnings}; from-url adds status/source_url).
    # Empty for every non-imported site — no migration.
    import_report: dict[str, Any] = Field(default_factory=dict)
    # Capture hardening config (mirrors sites_capture.SiteFormConfig fields).
    allowed_origins: list[str] = Field(default_factory=list)
    # Whether ``allowed_origins`` HARD-GATES lead capture, or is only a signal.
    #
    # Default OFF, deliberately, and this is the Formspree/Basin/Getform posture
    # rather than a relaxation of ours by accident. Two facts make the pin close to
    # worthless as a gate on the capture path while keeping all of its ability to
    # break a customer's contact form:
    #
    #   * The credential it guards is ALREADY PUBLIC on three of the four engines.
    #     html / react / static-svelte all ship ``paw_key`` as a hidden input in the
    #     page source, so "the signed key" is a site IDENTIFIER that anyone can read,
    #     not a secret the origin pin is protecting.
    #   * ``Origin`` binds BROWSERS ONLY. Any curl/script forges it in one flag, so
    #     the pin never stopped a determined spammer — it stopped the honest case.
    #
    # What it DID do reliably was 403 real submissions: a site whose doc predates
    # the deployed-host stamping, an async (react) build whose Site row was inserted
    # with ``url=""`` before the worker filled it in, an apex/``www.`` mismatch, a
    # preview URL, a page opened over ``file://`` (Origin: null). Every one of those
    # fails CLOSED, and on the native-form path the visitor — the customer's actual
    # prospect — is shown a raw JSON 403 instead of a thank-you page.
    #
    # So the controls that survive are the ones that work on a public endpoint with
    # a public key: the honeypot, the per-(scope, minute) rate limit, the injection
    # screen, and the payload cap. Origin becomes an ATTRIBUTABLE SIGNAL — recorded
    # on every lead (``LeadSource.origin``) so an owner can see where submissions
    # came from — and a per-site opt-in for anyone who wants the strict behaviour
    # back. Existing rows read the default and are therefore un-gated, which is the
    # intended migration: they were the ones silently dropping leads.
    enforce_origin: bool = False
    signed_key: str = ""
    rate_limit_per_min: int = 60
    per_ip_limit_per_min: int = 10
    honeypot_field: str = "company_website"
    event_mapping: dict[str, Any] = Field(default_factory=dict)
    domains: list[SiteDomain] = Field(default_factory=list)
    # Paw Bar concierge (T1): what a request authenticated with this site's
    # public embed key (``signed_key`` + ``allowed_origins``) is allowed to do.
    # The concierge resolver copies this onto the RequestContext.scopes, and the
    # per-endpoint ``require_scope`` gate checks against it. Defaults to the
    # concierge baseline; a foreign-site mint can narrow it. Existing (site-
    # capture) docs read this default but never consult it — the capture path is
    # its own gate — so the default is harmless for them.
    scopes: list[str] = Field(default_factory=lambda: ["chat", "kb.read", "event.ingest"])
    # Paw Bar concierge (T1): a kill switch for the embed key. The resolver fails
    # closed on it (a revoked key resolves to nothing) so a leaked key can be
    # cut off without deleting the Site. Defaults False (every existing doc is
    # live), so no migration.
    revoked: bool = False
    # Paw Bar concierge (D1 / SS-6): the OWNER's on/off kill switch for the
    # concierge — distinct from ``revoked`` (which cuts the KEY). When False, the
    # three public entry points (frame / chat / action) refuse with a 403, so the
    # owner can silence the bar instantly without deleting the Site or rotating the
    # key. Re-read on EVERY request (never cached on a warm client) so a toggle
    # takes effect immediately. Defaults True (every existing site stays live), so
    # no migration.
    concierge_enabled: bool = True
    # Paw Bar concierge (D1 / SS-6): the opening line the glass bar renders. Rides
    # into the frame's ``window.__PAWBAR__`` config as ``greeting``; the glass app
    # reads it in a parallel slice and falls back to its own default when "".
    # Defaults "" (no custom greeting), so no migration.
    concierge_greeting: str = ""
    # Paw Bar concierge (transcripts): the OWNER's retention switch for the
    # VISITOR half of a conversation. The agent's replies were always durable
    # (``ChatRunDoc.partial_text``); when this is True the visitor's own message is
    # persisted too (``ChatRunDoc.user_text``), which is what makes an owner-facing
    # transcript a real two-sided conversation instead of a monologue. It is a
    # separate switch because it is the only concierge setting that decides whether
    # NEW personal data is stored: a visitor types free text, so the stored line can
    # carry a name, an email, an order number. An owner running a
    # privacy-sensitive site turns it off and keeps the concierge — replies still
    # persist, only the visitor's words are dropped. Read per message (never
    # cached), so flipping it off stops collection on the very next turn; it does
    # NOT retroactively purge lines already stored. Defaults True (the transcript
    # the dashboard promises), so no migration.
    concierge_store_transcripts: bool = True
    # Paw Bar appearance (2026-08-19). The owner's white-label settings, rendered
    # into the frame's ``window.__PAWBAR__.tokens`` as ``--pawbar-*`` custom
    # properties. The widget has read that map since it shipped and the backend
    # answered ``{}`` the whole time; this is the other end of that wire.
    # Defaults reproduce today's look exactly, so an unstyled Site is unchanged
    # and there is no migration.
    concierge_appearance: ConciergeAppearance = Field(default_factory=ConciergeAppearance)
    # Site knowledge sync (``sites.kb_ingest``): the kb-go article ids this site's
    # own content currently occupies in ``pocket:<pocket_id>`` — the scope its
    # concierge reads. Kept so a later sync can delete the articles a renamed or
    # deleted page left behind WITHOUT touching the rest of the scope, which also
    # holds owner-uploaded files. Empty until the first sync, so no migration.
    kb_article_ids: list[str] = Field(default_factory=list)
    # When the last sync ran (success or not) and why it produced nothing, so the
    # dashboard can tell "this concierge has no knowledge yet" apart from "syncing
    # is broken". "" means the last sync was clean.
    kb_synced_at: datetime | None = None
    kb_sync_error: str = ""
    # SC-1: the stored URL of a screenshot of this site's live page — what the
    # gallery card renders instead of a title and three pills. Written by the
    # best-effort capture ``sites.screenshot`` schedules from the tail of a
    # successful deploy, via a targeted ``set`` so it can never roll back a
    # concurrent write. "" while no screenshot has landed (never deployed, no
    # public url, capture failed, Cloudflare unconfigured); the card falls back to
    # the text layout on empty, so this is never a gate on publishing.
    preview_image_url: str = ""
    # The site owner's record of WHO this site is for, and what they have billed
    # them. Two billing relationships meet on this document and they are not the
    # same one: ``plan_tier`` / ``subscription_status`` above are what the owner
    # pays US, while everything below is what the owner's OWN client owes THEM.
    # Only the first is a real charge; these four are an address book and a
    # receipt book that the Settings surface reads and writes.
    #
    # ``client_contact`` and ``client_notes`` are free text a human types about a
    # third party, so they hold personal data by design. They are workspace-scoped
    # like every other field here (read and write both go through ``_load``), never
    # reach a generated page, and are capped in the DTO rather than here so an
    # over-long value is a 422 at the edge instead of a silently truncated record.
    # All four default empty, so no migration.
    client_name: str = ""
    client_contact: str = ""
    client_notes: str = ""
    client_invoices: list[SiteInvoice] = Field(default_factory=list)

    def rotate_signed_key(self) -> str:
        """Regenerate the public embed key and return the new value (T1).

        Mints a fresh ``site_key_...`` token (the SAME format the sites service
        seeds — a world-visible, origin-bound embed key, NOT a hashed secret)
        and assigns it to ``self.signed_key``. Rotation is how a leaked embed
        key is retired: after this, the old key no longer resolves. The caller
        is responsible for persisting (``await site.save()``) — this mutates the
        in-memory doc only, mirroring how other model helpers stay I/O-free.
        """
        import secrets

        self.signed_key = f"site_key_{secrets.token_urlsafe(24)}"
        return self.signed_key

    class Settings:
        name = "sites"
        indexes = [
            [("workspace", 1), ("pocket_id", 1)],
            # T1: back the concierge key→Site lookup (resolve_site_key does a
            # ``find_one({"signed_key": key})`` on every concierge request). Not
            # unique: unpublished/foreign docs can share the "" default, and the
            # resolver rejects an empty key before it ever queries, so a blank
            # key never resolves against those rows.
            [("signed_key", 1)],
        ]
