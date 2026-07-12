# ee/pocketpaw_ee/sites/service.py — Sites control-plane orchestration. Sole
# owner of Site writes.
#
# Updated 2026-07-10 (HE-2 — canonical engine module): the engine content-selection
# checks now route through ``sites.engines`` predicates instead of inline
# ``== "svelte"`` string equality. ``is_source_engine(engine)`` replaces the
# "source vs rippleSpec" content switch (publish/activate promote, preview/audit
# fallback, dynamic-envelope), and ``content_key(engine)`` replaces the literal
# ``"source" | "rippleSpec"`` key pick. PURE refactor, zero behaviour change for the
# existing ripple/svelte engines. The three svelte NATIVE-EDITING guards
# (``apply_leaf_edits`` ~2595, native shadow-render ~2697, ``edit_svelte_component``
# ~2823) deliberately KEEP the ``!= "svelte"`` literal — their bodies are svelte-only
# (component-path splice, ``.svelte-kit/cloudflare`` read, DSV-5 source split), so
# guard and body must widen together; HE-9 widens ``apply_leaf_edits`` when the html
# editing lane lands.
#
# Updated 2026-07-09 (DP0-4 — publish async split + single-flight): ``_deploy_site_doc``
# now FORKS on ``_is_dynamic`` BEFORE any build. A DYNAMIC site no longer builds /
# deploys inline — it returns early into the new ``_provision_dynamic_site`` helper,
# which ensures the canonical Site doc in ``provision_status="provisioning"``
# (``deployed=False``, ``url=""``) and enqueues the durable ``provision_site`` job via
# ``jobs.service.dispatch_job`` (``params={}``). SINGLE-FLIGHT: a re-publish while the
# site is already ``provisioning`` does NOT enqueue a second job (a double-publish is a
# no-op). The enqueued job id rides the returned doc on the transient
# ``_provision_job_id`` PrivateAttr, which ``_to_response`` surfaces on
# ``SiteResponse.provision_job_id`` alongside ``provision_status``. The STATIC path is
# byte-for-byte the pre-DP0-4 inline build → deploy → upsert (regression guarantee).
# Both publish entry points reach it through ``_deploy_site_doc`` (the free ``publish``
# and the charge-first ``activate_site``), so both get the async behaviour; the return
# is still a plain ``_SiteDoc`` so their post-processing (plan stamp / sub-active) is
# unchanged.
#
# Updated 2026-07-02 (harden native-editing endpoints): (1) ``get_native_artifact``
# now routes its arm build through ``_build_or_cloud_error`` (like the publish paths)
# so a missing-toolchain / non-zero build / SmokeGateFailed becomes a clean
# CloudError (``sites.generator_failed``) instead of an opaque 500 (DEP-3); (2)
# ``apply_leaf_edits`` wraps the leaf-edit CLI bridge + result parse and maps its
# RuntimeError / KeyError / IndexError / TypeError to ``Internal("sites.leaf_edit_failed")``
# — a structured envelope, not a 500; and (3) it now SPLITS a dynamic svelte pocket's
# source envelope (``_split_svelte_source``) so the CLI receives ONLY the file map and
# the persist loop is CONFINED to the input file keyspace — a binding key
# (objects/sources/actions/auth) or a CLI-invented path is never written back as a
# component file.
#
# Updated 2026-07-01 (NE-5b — native-artifact endpoint): added
# ``get_native_artifact`` — the backend of the native shadow-render path. It ensures
# the pocket's ARMED svelte build (builder_origin set, so the paw-sites generator
# stamps ``data-uid`` on the editable leaves + embeds the ``paw-edit-manifest``
# script) via a DIRECT ``GeneratorClient.build`` (smoke=False, the same arm/preview
# gate ``make_site_editable`` uses) and returns the built ``<body>`` inner HTML +
# concatenated CSS as a dict, so the native editor can inject it into a shadow root
# instead of framing an iframe. The built static output is located by the returned
# ``BuildResult.project_dir`` + ``.svelte-kit/cloudflare/index.html`` — the SAME tree
# ``_default_bundle_reader`` reads ``_worker.js`` from and ``local_server`` copies to
# serve. A non-svelte pocket raises ValidationError (422); a missing / cross-tenant
# pocket surfaces the pockets service's NotFound / Forbidden (404 / 403).
# ``_generator`` + ``_read_built`` are injectable seams so the path is unit-testable
# without Bun / a real build.
#
# Updated 2026-07-01 (NE-4b — native-editing leaf-edit persist): added
# ``apply_leaf_edits`` — the backend of the native site-editing persist path. It
# splices the native editor's forwarded ``{uid, op}`` leaf edits into the pocket's
# svelte ``source`` map via ``generator_client.apply_leaf_edits`` (the paw-sites
# apply-leaf-edit CLI, a PURE transform — no build/workerd) and persists ONLY the
# changed files through ``pockets_service.set_svelte_source_file`` (each write
# auto-writes a Branch draft). Unlike ``edit_svelte_component`` it does NOT
# republish/rebuild — the editor already rendered the edit optimistically, so
# persisting the reviewable draft is the whole job. Empty edits / a non-svelte
# pocket raise ValidationError (422); a missing / cross-tenant pocket surfaces as
# the pockets service's NotFound / Forbidden (404 / 403). ``_apply`` is an
# injectable bridge seam so the path is unit-testable without Bun.
#
# Updated 2026-06-26 (feat/sites-dev-bridge-source, S1 — dev source carries the
# edit-bridge): ``dev_preview_pocket`` gained an optional ``builder_origin`` and
# threads it to ``get_manager().ensure_dev_server(builder_origin=...)`` so the
# dev-server-materialized SOURCE carries SE-1's section anchors + gated edit-bridge
# (the hover-edit overlay then works against the dev server, not only the static
# /editable build). The origin is resolved the SAME way ``make_site_editable``
# resolves it — the passed origin (the router's request ``Origin`` header) when
# present, else the ``_builder_origin()`` env fallback (PAW_SITES_BUILDER_ORIGIN) —
# so no new sourcing mechanism is invented. The dev path keeps ``static_build=False``
# (no prod build); only the generate/scaffold step needs the origin for the gated
# source injection, and an empty/unset origin still yields a non-bridged source (the
# generator's gate holds).
#
# Updated 2026-06-25 (feat/sites-workers-deploy-mode — workers.dev deploy mode):
# ``_deploy_site_doc`` now picks one of THREE deploy targets via a new
# ``_deploy_mode()`` helper reading PAW_CF_DEPLOY_MODE (``local`` | ``workers`` |
# ``wfp``): ``local`` → ``local_server.deploy_local`` (unchanged), ``workers`` →
# the NEW ``workers_deploy.deploy_workers`` (deploy a STATIC site as a regular
# Worker on the free workers.dev tier — a DYNAMIC site raises ``ValidationError``
# since it needs a per-tenant D1, Phase 2), ``wfp`` → ``cf.put_worker`` (the
# Workers-for-Platforms path, unchanged). When PAW_CF_DEPLOY_MODE is UNSET the
# LEGACY selection is preserved exactly (``_local_mode()`` → local, else WfP), and
# an injected ``_cloudflare`` still forces the WfP branch over an env-requested
# local mode — so the existing local/CF tests are unaffected. New
# ``_workers_deploy`` test-injection seam threads ``publish`` → ``_deploy_site_doc``
# (mirrors ``_local_deploy``/``_cloudflare``). Billing/charge-first logic untouched.
#
# Updated 2026-06-25 (feat/sites-cf-dispatch-worker — wire the published URL for the
# WfP serving layer): a user worker uploaded into the `paw-sites` dispatch namespace
# is NOT directly URL-addressable in Workers for Platforms — it serves only when the
# new Dynamic Dispatch Worker (ee/pocketpaw_ee/sites/cloudflare/dispatch-worker)
# routes `<site_id>.<PAW_CF_SITES_DOMAIN>` to it. So the CF branch of
# ``_deploy_site_doc`` now, AFTER ``put_worker`` succeeds, stamps the public URL as
# ``https://{site_id}.{PAW_CF_SITES_DOMAIN}`` when ``PAW_CF_SITES_DOMAIN`` is set; if
# it is unset it leaves ``url=""`` (the worker IS uploaded — the deploy succeeded —
# it is just unreachable until the operator deploys the dispatch worker + sets the
# domain) and logs a warning. The LOCAL branch is untouched. v1 is subdomain routing
# only; a connected custom hostname needs a hostname→site_id map in the dispatch
# worker (follow-up).
#
# Updated 2026-06-25 (feat/paw-sites-prod-deploy, DEP-3 — graceful generator
# failure): the publish path shells out to the paw-sites generator + bun, which in
# a misconfigured deploy (an image missing the toolchain) raised a bare
# FileNotFoundError / RuntimeError / SmokeGateFailed that escaped publish() as an
# UNHANDLED 500 (the cloud error handler maps ONLY CloudError). New helper
# ``_build_or_cloud_error`` wraps EVERY generator.build() call in the publish path
# (the live build in ``_deploy_site_doc`` and the preview build in ``publish``) and
# maps those build/install/smoke failures to ``Internal("sites.generator_failed")``
# (a clean 5xx envelope with a reason), chaining the cause for logs. A CloudError
# raised inside the build (an injected fake, the existing SmokeGateFailed-driven
# rollback callers) is re-raised unchanged so its own status/code stand.
#
# Updated 2026-06-24 (feat/billing-lifecycle — charge-first hardening, review
# loose ends A+B):
#   * (A) ``_publish_pending_site`` now CAPS the serialized deploy-input size
#     before persisting. A new module constant ``_MAX_PENDING_DEPLOY_INPUT_BYTES``
#     (4MB, well under Mongo's 16MB per-doc limit) bounds the rippleSpec / svelte
#     source snapshot captured on ``Site.pending_deploy_inputs``; an oversized
#     payload raises ``ValidationError("sites.deploy_inputs_too_large")`` BEFORE
#     any write or billing, instead of bloating the Site doc.
#   * (B) ``_publish_pending_site`` reordered to CHECKOUT-BEFORE-PERSIST. The
#     per-site Dodo checkout is opened FIRST (the ``site_id`` is deterministic via
#     ``_live_object_id``, so it rides the subscription metadata with no doc), then
#     the PENDING Site doc is upserted with ``subscription_id`` already set. A
#     checkout failure now PROPAGATES and creates NO pending doc — never an orphan
#     pending row with no ``subscription_id`` (the buyer retries). The existing-doc
#     signed_key reuse is preserved (read for the key only; not mutated/persisted
#     until checkout succeeds).
#   The companion (C) pending-reconciliation sweeper lives in the sibling
#   ``pending_sweeper.py`` (wired into the same heartbeat as the chat-runs sweeper)
#   for operator visibility of sites stuck pending past a threshold.
#
# Updated 2026-06-24 (feat/charge-first-sites — charge-first per-site publishing):
# a PAID site tier (positive ``annual_price_usd`` AND a configured
# ``dodo_product_id``) is now CHARGE-FIRST — published as PENDING and NOT deployed
# live until payment confirms; a FREE/base tier still deploys instantly.
#   * Extracted ``_deploy_site_doc`` — the generate + smoke-gate + Cloudflare/local
#     deploy + upsert-and-stamp-deployed half of ``publish``. ``publish`` (free
#     path) calls it as before; the charge-first activation reuses it at webhook
#     time. ``publish``'s preview branch is unchanged (it builds inline + returns a
#     transient doc, no deploy).
#   * ``publish_pocket`` resolves the per-site tier BEFORE deploy and branches:
#     PAID + chargeable → ``_publish_pending_site`` (create a PENDING Site doc with
#     ``deployed=False`` / ``subscription_status="pending"`` / ``plan_tier`` and the
#     captured deploy inputs on ``Site.pending_deploy_inputs``, open the Dodo annual
#     checkout, return the checkout_url — NO deploy); FREE/base → live publish as
#     today, then ``_apply_site_plan`` stamps the tier. A "paid" tier whose Dodo
#     product is UNCONFIGURED can't open a checkout, so it DEGRADES to an immediate
#     live publish (never strands the user).
#   * ``activate_site(site_id)`` — loads the PENDING site, runs ``_deploy_site_doc``
#     from the stored ``pending_deploy_inputs`` (the webhook carries only
#     workspace_id + site_id, and the pocket's draft may have advanced), marks the
#     sub active, promotes the draft to published, and emits ``SitePublished``. The
#     per-site ``subscription.active`` webhook calls it. Idempotent — an
#     already-active/deployed site is a no-op.
#   * The publish response carries ``checkout_url`` (``SiteResponse.checkout_url``):
#     the Dodo link for a paid publish, None for a free publish. It rides the
#     returned Site doc on a TRANSIENT ``_checkout_url`` PrivateAttr (never
#     persisted); ``_to_response`` reads it via getattr.
#
# Updated 2026-06-24 (C1 review fix — _cf_client guard): ``_cf_client`` reads the
# PAW_CF_* vars with ``os.environ.get`` and raises a ``ValidationError``
# (CloudError → 422) when any is missing, instead of a raw ``KeyError`` (an
# unhandled 500). ``add_domain`` calls it directly, so an unconfigured Cloudflare
# now returns a clean "Cloudflare is not configured" error.
#
# Updated 2026-06-24 (BC-9 — per-site annual plan + publish entitlement gate):
# ``publish_pocket`` gained ``site_plan_key`` (the per-site tier from
# ``billing.site_plans``, defaulting to the base tier) and now — after a LIVE
# publish persists the Site doc — stamps ``Site.plan_tier`` + ``Site.subscription_id``,
# opens a PER-SITE annual Dodo subscription via BC-7's ``create_subscription`` with
# ``metadata={workspace_id, site_id, plan_key}`` (the ``site_id`` is how the
# renewal webhook tells a per-site sub from a workspace-plan sub), and emits
# ``SitePublished``. When the site tier has no configured Dodo recurring product
# (v1 default) the sub init DEGRADES gracefully — the tier is recorded without a
# live charge, never crashing the publish. The publish ENTITLEMENT gate is the
# existing ``require_sites_plan`` (the "sites" plan feature) ``publish`` already
# runs FIRST, before any Site insert — a workspace lacking it raises Forbidden and
# no Site is created. New seam ``_billing_provider`` on ``publish_pocket`` injects
# a mock subscription provider in tests. A PREVIEW publish skips all of BC-9 (it
# never persists a Site doc).
#
# Updated 2026-06-24 (BC-10 — resell Cloudflare features by site-plan tier):
# ``add_domain`` now resolves the site's ``plan_tier`` → its
# ``site_plans.get_site_plan(...).cloudflare_features`` and passes that set to
# ``CloudflareClient.create_custom_hostname(hostname, features=...)``. So adding a
# custom domain to a HIGHER-tier site provisions its resold Cloudflare paid
# features (WAF / edge-cache / strict TLS) at hostname-create time; a base-tier
# (or unset-tier) site resolves to an empty set and stays on the basic create
# path, unchanged. ``site_plans`` is imported lazily inside ``add_domain``,
# mirroring ``publish_pocket``.
# Updated 2026-06-21 (DSV-2b — engine-appropriate objects read for svelte dynamic
# sites): the data-read resolver ``_dynamic_pocket_objects`` now selects the
# pocket's CONTENT ENVELOPE by engine before classifying / extracting ``objects``
# — for ``engine == "svelte"`` it reads the dynamic bindings
# (``objects``/``sources``/``actions``/``auth``) from the svelte ``source``
# envelope, for ripple (the default) from ``rippleSpec``, mirroring the
# ``version_content = (source if engine == "svelte" else ripple_spec)`` switch the
# publish/promote path already uses. Without this a dynamic SVELTE pocket (whose
# bindings live on ``source``, not ``rippleSpec``) showed NO tables in the Data
# tab. ``_is_dynamic`` / ``_dynamic_objects`` are unchanged — they already operate
# on "a content dict", so passing the engine-selected envelope is all that's
# needed; ripple dynamic sites keep reading from ``rippleSpec`` (no regress).
#
# Updated 2026-06-20 (DS-3 — control-plane read of a dynamic site's D1 data):
# added the operator data-view reads ``list_site_data_tables`` and
# ``read_site_data_table`` (backing GET /sites/by-pocket/{pocket_id}/data and
# .../data/{table}). The table LIST comes from the dynamic pocket spec's
# top-level ``objects`` (always available, even with no live D1); the per-table
# read runs a bounded, PARAMETERIZED ``SELECT * FROM <table> LIMIT ?`` over the
# per-tenant Cloudflare D1 via cloudflare_client.query_d1. SQL safety: the table
# identifier is validated against the spec's declared object names (an unknown
# table → 404, never interpolated), and every value binds through ``params``. A
# NON-dynamic pocket → 400 ("sites.not_dynamic"). Local/dev mode
# (``_local_mode()``) has no live D1, so the read DEGRADES cleanly — it returns
# ``available=False`` / ``reason="live_on_cloudflare_only"`` with the schema still
# listed from the spec (no error). Self-contained ``_is_dynamic`` /
# ``_derive_d1_database_id`` helpers mirror the sibling DS-2 (feat/sites-d1-
# bindings) branch so the READ targets the SAME D1 a deploy binds, but this branch
# does NOT depend on DS-2's code (it reads Site.d1_database_id via getattr with an
# empty default, else derives the id) so it builds green on its own.
# Updated 2026-06-20 (DS-1a — surface dynamic-site pattern): list_for_workspace()
# and pocket_status() now carry the SOURCE pocket's authoring ``pattern``
# ("dynamic" | "landing" | ...) on their responses so the frontend can badge
# dynamic sites. The pattern lives on Pocket.pattern, not the Site, so it is
# resolved via pockets_service.patterns_for_pockets — ONE batch read for the list
# (no N+1), a single-id read for status — keeping the Pocket read on the pockets
# side (entity isolation; this service never imports the Pocket model). _to_response
# gained an optional ``pattern`` arg; both DTOs default it to "" (empty-safe for a
# pocket with no pattern or a missing/cross-tenant pocket).
#
# Updated 2026-07-09 (SR-9 — surface each site's ENGINE): the sibling of DS-1a.
# list_for_workspace() and pocket_status() now also carry the source pocket's
# authoring ``engine`` ("svelte" | "ripple") so the gallery can badge each card's
# engine (Custom vs Ripple) without a per-site fetch. Resolved via the new
# pockets_service.engines_for_pockets (one projected $in read, tenant-scoped),
# mirroring patterns_for_pockets. _to_response gained an optional ``engine`` arg;
# both DTOs default it to "" (empty-safe for a pocket that predates the engine
# field or a missing/cross-tenant pocket).
#
# Updated 2026-06-19 (P2b-backend — "Last Deployed" + revert endpoint): publish()
# now stamps the Site doc's ``deployed_at`` (UTC) ONLY when a non-preview deploy
# succeeds (when ``deployed`` flips True) — the true "last shipped" marker, not a
# "last touched" one. ``_to_response``/``pocket_status`` surface it as an ISO
# string|None on the DTOs. Added ``revert_pocket_version`` — resolves a pocket's
# version_no → its ArtifactVersion row (tenant-scoped, main branch) and calls
# ``versions.revert`` to write a NEW forward-moving draft from that version's
# content (the normal review/publish flow then applies); backs the new
# POST /sites/by-pocket/{pocket_id}/versions/{version_no}/revert endpoint.
#
# Updated 2026-06-19 (P0b — review-400 self-heal): ``request_publish_pocket`` no
# longer 400s a LEGACY site (one published before BP-1, so it has ZERO
# ``artifact_versions`` rows and no draft). When ``get_draft`` is None it now
# BACKFILLS a draft snapshot of the pocket's current content via the existing
# ``_ensure_pocket_draft`` helper, then re-reads; the 400 is kept ONLY for a
# genuinely empty / nonexistent pocket or a foreign-workspace draft. This fixes
# the "Submit for review → 400 (and edits seem to go live)" bug — both symptoms
# were the same missing-draft-lineage gap.
#
# Updated 2026-06-18 (feat/sites-smoke-at-publish, PERF-4): publish() now threads
# ``smoke=not preview`` into generator.build(), so the workerd SMOKE render runs
# ONLY for a LIVE publish (preview=False) and is SKIPPED for a preview/edit/arm
# build (preview=True). The render is per-edit overhead only needed before a
# deploy; skipping it cuts the remaining per-edit cost left after PERF-3 cached
# `bun install`. The live publish keeps the gate AND the edit_svelte_component
# rollback-on-SmokeGateFailed behaviour unchanged. A preview that would fail smoke
# is no longer blocked — acceptable, because the live publish still gates + rolls
# back, so a broken edit can never reach the live deploy.
#
# Updated 2026-06-18 (feat/sites-cached-build, PERF-3): publish() now forwards the
# source pocket_id to generator.build() so the build runs in the STABLE per-pocket
# working dir (persistent node_modules + cached `bun install`), cutting the dominant
# per-edit cost across both preview and live publishes. A site_id is still minted
# fresh per publish — only the on-disk build dir is reused per pocket.
#
# Updated 2026-06-17 (fix/sites-plan-gate-asymmetry): added require_sites_plan()
# and call it at the top of publish() AND publish_pocket(). Sites is the "sites"
# plan feature (go+); the REST router gates it with require_plan_feature("sites"),
# but the chat agent created + published sites IN-PROCESS via the sites_manager MCP
# tools, which bypass the HTTP router. A free-plan workspace could therefore
# deploy a live site that GET /sites then 403'd (write path ungated, read path
# gated). The guard reads the plan from workspace_service.get_workspace_plan
# against guards.abac.PLAN_FEATURES (same source of truth as the HTTP gate) and
# raises Forbidden('plan.feature_denied') before any pocket read / generate /
# deploy. Every publish path (REST + MCP publish + direct callers) funnels
# through publish(), so this one call covers them all; the create MCP handlers
# call require_sites_plan() directly (they reach agent_create, not this service).
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
# Updated 2026-06-18 (fix/sites-edit-draft-not-publish): EDITING a site no longer
# auto-publishes it. ``publish()`` gained a ``preview`` flag (forwarded through
# ``publish_pocket``); ``edit_svelte_component`` and ``make_site_editable`` now
# call it with ``preview=True``. A preview build still smoke-gates + locally serves
# the working copy (so a broken edit is caught), but it does NOT promote the
# pocket's draft to ``published`` and does NOT overwrite the canonical live Site
# doc/url — it returns a TRANSIENT preview Site (``deployed=False``). Before this
# fix both edit-path callers routed through ``publish`` → promote+deploy, so after
# any edit the pocket had only published versions and NO draft; ``get_draft``
# returned None and ``request_publish_pocket`` (Submit-for-review) raised → the UI
# got a 400. Now the draft survives, the published pointer is unchanged, and only
# an approved review (the real ``publish``, ``preview=False``) deploys live +
# promotes. ``make_site_editable`` also ensures a draft snapshot exists on arm
# (``_ensure_pocket_draft``) so a never-edited armed site still has a working copy
# to frame + submit. The chat-CREATE publish and the approve→publish executor are
# unchanged (they stay ``preview=False`` real publishes).
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
# Updated 2026-06-18 (feat/branch-primitive-audit, BP-7 — producer 2): added
# audit_pocket(). It reads the pocket's content the SAME way preview_pocket does
# (draft-version snapshot, falling back to current rippleSpec/source) and runs the
# pure deterministic audit engine (sites.audit.audit_pocket_site) over it. Each
# finding carries a ``fix_prompt`` the UI feeds to the EXISTING edit path
# (edit_svelte_component / refine) so a fix lands as a reviewable draft in the
# Tray — BP-7 adds NO apply endpoint.
#
# Updated 2026-06-18 (feat/sites-stable-identity, PERF-1): a LIVE publish now has a
# STABLE per-(workspace, pocket_id) identity instead of minting a fresh ObjectId per
# call (which inserted a NEW Site doc at a NEW folder/URL every publish — one pocket
# had 14 docs, the gallery showed dupes, and pocket_status returned an arbitrary
# stale doc with url=None: the stale-live-link bug). Mirroring the preview path's
# _preview_id:
#   * _live_object_id(workspace, pocket) derives a deterministic ObjectId from the
#     pair, used for the deploy folder/URL, the CF Worker script_name (overwrite the
#     worker, no orphan), and the Site doc _id. publish()'s preview branch is
#     unchanged (it already serves at the stable preview-<pocket> path).
#   * publish() UPSERTS ONE canonical Site doc keyed on the stable _id (find-then-
#     save/insert) — re-publish refreshes the deploy fields in place and PRESERVES
#     domain/allowed_origins/signed_key, instead of inserting a second row.
#   * pocket_status() reads the CANONICAL doc via _canonical_site_doc (the stable-id
#     doc, else the newest with a real url) and returns its non-null url + is_live,
#     dropping the arbitrary find_one. PERF-1 does NOT migrate pre-existing dupes
#     (that is PERF-2) — _canonical_site_doc just resolves the live one among them.
#
# Updated 2026-06-18 (feat/sites-diff-edit, P3 — TARGETED / DIFF edit): a svelte
# component edit can now be expressed as a list of search/replace blocks
# (``edits=[{old_string, new_string}, ...]``, like the built-in Edit tool) INSTEAD
# of the FULL ``new_source``, so the agent emits ONLY the change for a small edit
# ("add a bg color to the nav") rather than reading + regenerating the whole file
# — far fewer tokens in and out, the dominant edit-latency cost. New pieces:
#   * apply_edits(source, edits) — a PURE, I/O-free function that applies the
#     blocks sequentially; each ``old_string`` must match EXACTLY ONCE (0 or >1
#     raises ValidationError with a clear, retry-able message), so it is the same
#     uniqueness contract the built-in Edit tool enforces.
#   * edit_svelte_component() gained an ``edits`` param (alternative to
#     ``new_source``). When ``edits`` is given it reads the pocket's CURRENT
#     component source via the pockets service, computes the new source with
#     apply_edits, and hands that to the UNCHANGED SE-2 persist + preview/republish
#     + smoke-gate-rollback path. ``new_source`` (full rewrite) is unchanged and
#     stays the fallback for large rewrites; exactly one of the two must be given.

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    Forbidden,
    Internal,
    NotFound,
    ValidationError,
    with_cause,
)
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.models.site import SiteDomain as _SiteDomainDoc
from pocketpaw_ee.sites.domain import HostnameStatus
from pocketpaw_ee.sites.dto import (
    AuditFinding,
    AuditResponse,
    DevPreviewResponse,
    DomainStatusResponse,
    SiteDataRowsResponse,
    SiteDataTableInfo,
    SiteDataTablesResponse,
    SitePreviewResponse,
    SiteResponse,
    SiteStatusResponse,
)
from pocketpaw_ee.sites.engines import content_key, is_source_engine
from pocketpaw_ee.sites.generator_client import GeneratorClient

logger = logging.getLogger(__name__)

# The control plane reads the Worker bundle adapter-cloudflare emits here.
_WORKER_BUNDLE_REL = ".svelte-kit/cloudflare/_worker.js"

# The prerendered static site adapter-cloudflare emits (index.html + the
# ``_app/...`` assets) — the SAME tree ``_default_bundle_reader`` reads
# ``_worker.js`` from and ``local_server.persist_site`` copies to serve. NE-5b reads
# the armed build's index.html + CSS from here.
_CLOUDFLARE_BUILD_REL = ".svelte-kit/cloudflare"

# BP-2: the source pocket is the versionable artifact behind a site. The Branch
# primitive (BP-1) keys every version on (scope_type, scope_id); for a site the
# scope is the pocket it is published from.
_VERSION_SCOPE_TYPE = "pocket"

# Sites is a plan-gated feature: it unlocks with the "sites" plan feature (go+ on
# the consumer ladder — Paw Go gets a site). NOTE: this used to be the "fabric"
# flag, but that flag was overloaded (it also gated the enterprise-only Fabric
# ontology), so Sites + Leads were decoupled onto their own "sites" flag. The REST
# router (sites/router.py) gates every endpoint with require_plan_feature("sites"),
# but the chat agent creates + publishes sites IN-PROCESS via the sites_manager MCP
# tools, which never pass through that HTTP router. Without a service-level gate a
# free-plan workspace could create and deploy a live site that GET /sites then
# 403'd — a created-but-invisible resource. require_sites_plan() closes that
# asymmetry at the service chokepoint so the in-process write paths are gated
# identically to HTTP.
_SITES_PLAN_FEATURE = "sites"

# charge-first (review fix A): cap the serialized size of the deploy inputs a
# PENDING paid site stores on ``Site.pending_deploy_inputs``. A pathological
# rippleSpec / svelte source map (e.g. a huge inlined data blob) could otherwise
# bloat the Site doc toward Mongo's 16MB per-document hard limit. We reject well
# under that ceiling (4MB) so the captured snapshot — plus every other field on the
# Site doc — always fits, and the failure surfaces as a clear 422 at publish time
# rather than an opaque BSON write error (or a doc that can never be updated again).
_MAX_PENDING_DEPLOY_INPUT_BYTES = 4_000_000


def _default_bundle_reader(project_dir: str) -> bytes:
    return Path(project_dir, _WORKER_BUNDLE_REL).read_bytes()


def _extract_body_inner(html: str) -> str:
    """Return the INNER HTML of the built page's ``<body>`` (NE-5b).

    The prerendered ``index.html`` wraps the data-uid-stamped leaves + the embedded
    ``<script id="paw-edit-manifest">`` in ``<body>…</body>``; the native editor
    injects only that inner markup into a shadow root, so the ``<html>``/``<head>``
    chrome is stripped. Falls back to the whole document if (defensively) no body tag
    is present — a prerendered SvelteKit page always has one."""
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else html).strip()


def _extract_css(html: str, cloudflare_dir: Path) -> str:
    """Concatenate the built page's CSS into ONE string the native editor injects as
    a single ``<style>`` (NE-5b).

    Collects, in document order: (1) any inline ``<style>…</style>`` blocks
    (SvelteKit can inline critical CSS), then (2) every ``<link rel="stylesheet">``
    stylesheet, read from disk under the built ``.svelte-kit/cloudflare/`` tree. The
    prerendered index links assets with either a RELATIVE (``./_app/…``) or ABSOLUTE
    (``/_app/…``) href, so both are resolved against the build dir. Each resolved
    path is contained to the build tree (a ``../`` traversal in a hand-authored
    component's link is refused) before it is read."""
    parts: list[str] = []
    for style in re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.IGNORECASE):
        if style.strip():
            parts.append(style.strip())

    root = cloudflare_dir.resolve()
    for tag in re.findall(r"<link\b[^>]*>", html, re.IGNORECASE):
        if not re.search(r"""rel\s*=\s*["']?stylesheet""", tag, re.IGNORECASE):
            continue
        href_m = re.search(r"""href\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
        if not href_m:
            continue
        rel = href_m.group(1).split("?", 1)[0].split("#", 1)[0]
        if rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        if not rel:
            continue
        css_path = (cloudflare_dir / rel).resolve()
        # Refuse to read outside the built output tree (defensive — the hrefs come
        # from our own generator, but a component author controls the leaf markup).
        try:
            css_path.relative_to(root)
        except ValueError:
            continue
        if css_path.is_file():
            text = css_path.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _read_native_artifact(project_dir: str) -> tuple[str, str]:
    """Read the armed build's ``<body>`` inner HTML + concatenated CSS from the built
    static output (NE-5b) — the default ``_read_built`` seam.

    ``project_dir`` is the ``BuildResult.project_dir`` the generator returns; the
    prerendered site lives under ``<project_dir>/.svelte-kit/cloudflare/`` (the SAME
    tree ``_default_bundle_reader`` / ``local_server`` read). Returns
    ``(body_html, css)``."""
    cloudflare_dir = Path(project_dir, _CLOUDFLARE_BUILD_REL)
    html = (cloudflare_dir / "index.html").read_text(encoding="utf-8")
    return _extract_body_inner(html), _extract_css(html, cloudflare_dir)


async def _build_or_cloud_error(
    generator: GeneratorClient, *, map_smoke_gate: bool = True, **build_kwargs: Any
) -> Any:
    """Run ``generator.build(...)`` and map any build/install/smoke failure to a
    ``CloudError`` so a publish never escapes as an UNHANDLED 500 (DEP-3).

    The generator shells out to the paw-sites generator (paw-sites-gen) + bun. In a
    misconfigured deploy (e.g. an image missing the toolchain — the bug this fix
    addresses) ``build()`` raises a bare ``FileNotFoundError`` (no such binary on
    PATH); on a non-zero generator step it raises ``RuntimeError``; a failed install
    or the workerd SSR fail-gate raises ``SmokeGateFailed`` (a ``RuntimeError``
    subclass). None of those are ``CloudError`` subclasses, so the cloud error
    handler (which maps ONLY ``CloudError``) would let them surface as an opaque 500
    with no machine-readable code. This maps them to ``Internal`` (``CloudError`` →
    500 with code ``sites.generator_failed``) so the API returns a clean envelope
    with a reason instead — and chains the cause for log context (the cause is never
    leaked in the client envelope). ``CloudError`` raised inside the build is
    re-raised unchanged so its own status/code/message stand.

    ``map_smoke_gate`` (default True) controls whether ``SmokeGateFailed`` is mapped
    too. The LIVE publish path (``_deploy_site_doc``) maps it — nothing downstream
    catches it there, so it would escape as a 500. The PREVIEW/edit path passes
    ``map_smoke_gate=False`` so ``SmokeGateFailed`` propagates RAW: the edit caller
    (``edit_svelte_component``) catches it to roll the component source back to its
    prior contents, a contract that must be preserved (mapping it to ``Internal``
    would silently break that rollback)."""
    from pocketpaw_ee.sites.generator_client import SmokeGateFailed

    try:
        return await generator.build(**build_kwargs)
    except CloudError:
        # Already a clean envelope — let it stand (status/code/message preserved).
        raise
    except SmokeGateFailed as exc:
        if not map_smoke_gate:
            # Preview/edit path: let the caller's rollback-on-SmokeGateFailed run.
            raise
        logger.error("sites.publish: generator smoke gate failed", exc_info=True)
        raise with_cause(
            Internal(
                "sites.generator_failed",
                "Site generation failed — the publishing toolchain is unavailable "
                "or the build did not complete. See server logs for details.",
            ),
            exc,
        ) from exc
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        # Missing toolchain / non-zero generator step → a real server-side infra
        # failure, so a 5xx with a reason (not a 4xx — the request was well-formed).
        logger.error("sites.publish: generator build failed", exc_info=True)
        raise with_cause(
            Internal(
                "sites.generator_failed",
                "Site generation failed — the publishing toolchain is unavailable "
                "or the build did not complete. See server logs for details.",
            ),
            exc,
        ) from exc


def _preview_id(pocket_id: str) -> str:
    """A STABLE per-pocket id for serving a preview build (the EDIT/arm path).

    A live publish derives a stable per-pocket ObjectId (``_live_object_id``); a
    preview must likewise serve at the SAME URL across repeated builds so the
    builder iframe can frame it once and just reload — otherwise every edit/arm
    builds at a new ``/<minted-id>/`` and the user never sees the change (the churn
    bug). ``local_server.persist_site`` overwrites ``<home>/<id>/`` in place, so a
    deterministic id derived from the pocket gives the same URL with fresh content
    on each preview build. Prefixed ``preview-`` so a preview dir never collides
    with a live site's dir."""
    return f"preview-{pocket_id}"


def _live_object_id(workspace_id: str, pocket_id: str) -> ObjectId:
    """A STABLE per-(workspace, pocket) ObjectId for the LIVE published site (PERF-1).

    Before PERF-1 ``publish`` minted ``ObjectId()`` per call, so every publish
    inserted a NEW Site doc at a NEW deploy folder / URL — one pocket accumulated 14
    Site docs, the gallery showed dupes, and ``pocket_status`` did an arbitrary
    ``find_one`` across them (the stale-live-link bug). Mirroring ``_preview_id``,
    the live site now has a STABLE identity derived deterministically from
    ``(workspace_id, pocket_id)``: the SAME 12-byte ObjectId every publish, so:

      * the deploy folder / URL is stable (``local_server.persist_site`` overwrites
        ``<home>/<id>/`` in place ⇒ same URL, fresh content);
      * the CF Worker ``script_name`` (== this id) is stable ⇒ ``put_worker``
        OVERWRITES the worker per pocket instead of orphaning the old one;
      * the Site doc ``_id`` is stable ⇒ publish UPSERTS ONE canonical doc per
        ``(workspace, pocket_id)`` rather than inserting a fresh row each time, and
        ``script_name == str(site.id)`` still holds.

    The id is the first 12 bytes of ``sha1(workspace_id:pocket_id)`` — a pure
    function of the pair, collision-resistant across the id space the same way a
    minted ObjectId is, and never colliding with a ``preview-<pocket>`` dir (those
    are strings, not ObjectId hex)."""
    import hashlib

    digest = hashlib.sha1(f"{workspace_id}:{pocket_id}".encode()).digest()
    return ObjectId(digest[:12])


# DS-2: the Worker binding name a dynamic site reads its D1 through. Must match
# the generator's wrangler.toml binding (``binding = "DB"``) so the compiled
# remote functions (which reference ``env.DB``) resolve.
_D1_BINDING_NAME = "DB"


# DS-3: the row cap for a table read. The data-view lists recent records, not a
# full export — a bounded read keeps the control-plane call cheap and the UI
# responsive. Overridable via PAW_SITES_DATA_ROW_LIMIT.
_DATA_ROW_LIMIT_DEFAULT = 200


def _data_row_limit() -> int:
    """The max rows a single table read returns (DS-3). Bounded so the operator
    data-view stays a recent-records list, not an unbounded export. Reads
    PAW_SITES_DATA_ROW_LIMIT (a positive int) and falls back to the default for an
    unset / malformed value."""
    import os

    raw = os.environ.get("PAW_SITES_DATA_ROW_LIMIT")
    try:
        n = int(raw) if raw else _DATA_ROW_LIMIT_DEFAULT
    except (TypeError, ValueError):
        return _DATA_ROW_LIMIT_DEFAULT
    return n if n > 0 else _DATA_ROW_LIMIT_DEFAULT


def _is_dynamic(pattern: str | None, ripple_spec: dict[str, Any] | None) -> bool:
    """Classify a pocket as a DYNAMIC site (DS-3, self-contained — does NOT depend
    on DS-2's copy of this helper).

    ``pattern == "dynamic"`` is authoritative (the create-dynamic-site tool stamps
    it). As a safety net — for a pocket that carries dynamic bindings but was not
    stamped — a spec declaring any top-level ``sources`` / ``actions`` (or
    ``auth``) is also dynamic, mirroring the generator's own classifier. Anything
    else (a static landing / brochure pocket) is NOT dynamic and has no D1 to
    read."""
    if pattern == "dynamic":
        return True
    if not isinstance(ripple_spec, dict):
        return False
    return bool(ripple_spec.get("sources") or ripple_spec.get("actions") or ripple_spec.get("auth"))


def _dynamic_objects(ripple_spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The declared tables (the spec's top-level ``objects``) of a dynamic site.

    ``objects`` is an ARRAY of ``{name, fields, primaryKey}`` table definitions
    (the dynamic-site authoring shape — see the create-dynamic-site skill). The D1
    migration is derived from these, so they are the AUTHORITATIVE set of tables a
    control-plane read may touch. Returns only well-formed entries (a dict with a
    non-empty string ``name``); a spec with no ``objects`` returns an empty list."""
    if not isinstance(ripple_spec, dict):
        return []
    raw = ripple_spec.get("objects")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for obj in raw:
        if isinstance(obj, dict) and isinstance(obj.get("name"), str) and obj.get("name"):
            out.append(obj)
    return out


def _derive_d1_database_id(workspace_id: str, pocket_id: str) -> str:
    """A STABLE D1 database id for a dynamic site, derived from
    ``(workspace_id, pocket_id)`` (DS-3, self-contained).

    DS-2 (feat/sites-d1-bindings) introduces ``Site.d1_database_id`` and an
    identically-shaped derive helper for the DEPLOY (bind) path. This branch is
    off dev and does NOT have DS-2 yet, so to build green on its own it resolves
    the D1 id the SAME way: read the Site doc's ``d1_database_id`` when present
    (via getattr with an empty default), else derive it deterministically from the
    pair. The derivation must match DS-2's exactly so the READ targets the SAME
    database the DEPLOY bound; both hash ``"d1:{workspace}:{pocket}"`` into a UUID.

    Shaped like a Cloudflare D1 id (a UUID) so it slots into the query path
    unchanged once a real provisioner persists Cloudflare's returned id on the
    Site doc (the getattr read picks that up automatically)."""
    import hashlib
    import uuid

    digest = hashlib.sha1(f"d1:{workspace_id}:{pocket_id}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16]))


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
    """Build the real Cloudflare client from settings (env). Injected in tests.

    C1 review fix — reads the required vars with ``os.environ.get`` and raises a
    ``ValidationError`` (CloudError → 422, mapped by the cloud error handler) when
    any is missing, instead of a raw ``KeyError`` (which surfaces as an unhandled
    500). ``add_domain`` calls this directly, so an unconfigured Cloudflare now
    returns a clean "Cloudflare is not configured" error rather than a 500.
    """
    import os

    from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

    account_id = os.environ.get("PAW_CF_ACCOUNT_ID")
    api_token = os.environ.get("PAW_CF_API_TOKEN")
    zone_id = os.environ.get("PAW_CF_ZONE_ID")
    if not (account_id and api_token and zone_id):
        raise ValidationError("sites.cloudflare_unconfigured", "Cloudflare is not configured")

    return CloudflareClient(
        account_id=account_id,
        api_token=api_token,
        zone_id=zone_id,
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


def _deploy_mode() -> str | None:
    """The EXPLICIT deploy target for a live publish, read from PAW_CF_DEPLOY_MODE
    (``local`` | ``workers`` | ``wfp``), or ``None`` when unset.

    A 3-way selector layered OVER the existing local/Cloudflare decision without
    disturbing it:
      * ``local``   → serve the static site from localhost (local_server.deploy_local).
      * ``workers`` → deploy as a regular Worker on the free workers.dev tier
                      (workers_deploy.deploy_workers — STATIC sites only; a dynamic
                      site raises rather than deploying a broken site).
      * ``wfp``     → the Workers-for-Platforms dispatch-namespace path
                      (cloudflare_client.put_worker) — today's Cloudflare default.
      * UNSET (``None``) → PRESERVE today's behaviour: ``_local_mode()`` selects the
                      local branch, else the cf/put_worker (wfp) path. So nothing
                      changes for an environment that does not set the var.

    A value other than the three known modes is treated as UNSET (logged) so a typo
    degrades to the safe legacy behaviour rather than failing the publish."""
    import os

    raw = (os.environ.get("PAW_CF_DEPLOY_MODE") or "").strip().lower()
    if not raw:
        return None
    if raw in ("local", "workers", "wfp"):
        return raw
    logger.warning(
        "sites: unknown PAW_CF_DEPLOY_MODE=%r — falling back to legacy local/wfp selection",
        raw,
    )
    return None


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


def _to_response(doc: _SiteDoc, pattern: str = "", engine: str = "") -> SiteResponse:
    deployed_at = getattr(doc, "deployed_at", None)
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
        # P2b: ISO string of the last successful live deploy, or None before the
        # first deploy (pre-P2b rows read null via the getattr default).
        deployed_at=deployed_at.isoformat() if deployed_at is not None else None,
        # DS-1a: the source pocket's authoring pattern ("dynamic" | "landing" |
        # ...), resolved by the caller from Pocket.pattern (it lives on the pocket,
        # not the Site). "" when unset / unresolved so the gallery is empty-safe.
        pattern=pattern,
        # SR-9: the source pocket's authoring engine ("svelte" | "ripple"),
        # resolved by the caller from Pocket.engine (sibling of pattern above). ""
        # when unresolved, so the gallery's engine badge is empty-safe.
        engine=engine,
        # charge-first: the Dodo checkout link a PAID-tier publish returns, read
        # from the transient ``_checkout_url`` PrivateAttr (never persisted). None
        # for a free/live publish and for any list/status read (those docs are
        # loaded from Mongo, where the PrivateAttr defaults to None).
        checkout_url=getattr(doc, "_checkout_url", None),
        # DP0-4: the dynamic-site provision state (persisted) + the id of the job a
        # dynamic publish just enqueued (transient ``_provision_job_id`` PrivateAttr,
        # None for a static publish / any DB-loaded doc / a single-flight no-op).
        provision_status=getattr(doc, "provision_status", "none"),
        provision_job_id=getattr(doc, "_provision_job_id", None),
    )


async def require_sites_plan(workspace_id: str) -> None:
    """Raise cloud Forbidden('plan.feature_denied') unless the workspace's plan
    includes the Sites ("sites") feature.

    The shared plan gate for the in-process site write paths (publish + the
    create MCP handlers). Reads the plan with the SAME source of truth
    (``workspace_service.get_workspace_plan``) and the SAME feature table
    (``guards.abac.PLAN_FEATURES``) as the HTTP ``require_plan_feature("sites")``
    dependency, so a free-plan caller is denied identically whether it arrives
    over REST or through the chat agent. A missing workspace surfaces as NotFound
    (mirroring the HTTP gate), and the error message names the minimum plan that
    unlocks Sites. Imports are local to keep the sites service importable without
    eagerly pulling the cloud workspace/guards modules."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service
    from pocketpaw_ee.guards.abac import PLAN_FEATURES

    plan = await workspace_service.get_workspace_plan(workspace_id)
    if plan is None:
        raise NotFound("workspace", workspace_id)
    if _SITES_PLAN_FEATURE not in PLAN_FEATURES.get(plan, set()):
        # Name the minimum plan that unlocks the feature, like the HTTP gate.
        # Walks the consumer ladder cheapest-first; ``sites`` lives on go+, so
        # this resolves to "Go".
        needed = next(
            (
                p
                for p in ("free", "go", "pro", "pro_max", "enterprise")
                if _SITES_PLAN_FEATURE in PLAN_FEATURES.get(p, set())
            ),
            "go",
        )
        raise Forbidden(
            "plan.feature_denied",
            f"Sites requires the {needed.capitalize()} plan — upgrade, or switch "
            "to a workspace that has it.",
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
    pattern: str | None = None,
    builder_origin: str | None = None,
    preview: bool = False,
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
    _workers_deploy: Callable[[str, str], Any] | None = None,
) -> _SiteDoc:
    """Generate, smoke-gate, deploy, and persist a site. Raises SmokeGateFailed
    (from generator_client) if the workerd smoke render fails — the site is not
    deployed and not persisted as deployed.

    PREVIEW MODE (Branch primitive — the EDIT/arm path). When ``preview=True``,
    this builds + smoke-gates + locally serves a DRAFT preview but does NOT take
    the edit live: it does NOT promote the pocket's draft version to ``published``
    and it does NOT claim/overwrite the canonical live Site doc. It returns a
    transient Site-shaped object whose ``url`` is the preview URL (with
    ``deployed=False``) so the builder iframe can frame the working copy, while the
    pocket's draft survives for review (``get_draft`` stays non-None, the
    ``published`` pointer is unchanged) and ``request_publish_pocket`` can submit
    it. Only an approved review (the real ``publish``, ``preview=False``) deploys
    live + promotes. ``preview=True`` requires ``_local_mode()`` / no injected CF
    deploy claim — the preview build is served from localhost (never the CF live
    deploy); a CF-only preview build is still generated and smoke-gated but is not
    PUT into the dispatch namespace.

    The generate + deploy + upsert half lives in ``_deploy_site_doc`` (extracted so
    the charge-first webhook ``activate_site`` can run the SAME deferred deploy at
    payment-confirm time). It has two branches:
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
    non-editable site (empty ``builder_origin`` on the doc, no bridge).

    Gated on the workspace's plan: Sites is the "sites" feature (go+), so a
    free-plan workspace is rejected with Forbidden('plan.feature_denied') here —
    BEFORE any pocket read, generation, or deploy. Both ``publish_pocket`` (REST +
    MCP) and direct service callers funnel through ``publish``, so this one gate
    covers every in-process publish path."""
    # Plan gate FIRST — before any pocket read, name resolution, generation, or
    # deploy — so a free-plan caller is denied identically to the HTTP router's
    # require_plan_feature("sites") gate. Every in-process publish path (REST,
    # MCP publish, direct callers) funnels through here.
    await require_sites_plan(workspace_id)

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

    # PERF-1: the LIVE site has a STABLE per-(workspace, pocket) id (mirroring the
    # preview path's _preview_id), so re-publishing a pocket overwrites the SAME
    # deploy folder / URL / CF worker / Site doc in place instead of minting a fresh
    # one each call. A PREVIEW build still uses the freshly-minted ObjectId for its
    # transient (never-persisted) doc id and serves at the stable preview path.
    site_id = str(ObjectId()) if preview else str(_live_object_id(workspace_id, pocket_id))
    # PERF-1 fix (review finding): on a live RE-publish the upsert below preserves
    # the stored ``doc.signed_key``, so minting a fresh key here would bake a
    # ``captureSignedKey`` into the built HTML that no longer matches the doc the
    # capture endpoint verifies against — silently breaking lead capture on every
    # re-publish. Reuse the existing site's key when one is already stored; mint a
    # new key only for a first publish or a preview (which never persists a doc).
    signed_key = f"site_key_{secrets.token_urlsafe(24)}"
    if not preview:
        _existing = await _SiteDoc.find_one(
            {"_id": _live_object_id(workspace_id, pocket_id), "workspace": workspace_id}
        )
        if _existing is not None and _existing.signed_key:
            signed_key = _existing.signed_key

    # PREVIEW MODE (Branch primitive — EDIT/arm path): build + smoke-gate + locally
    # serve a DRAFT preview but do NOT take the edit live. The build runs the smoke
    # gate (a broken edit is caught BEFORE it can be served), then we serve the built
    # dir from localhost and return a TRANSIENT Site-shaped object (NOT persisted,
    # ``deployed=False``) carrying that preview URL. The pocket's draft version is
    # left untouched (no promote → ``get_draft`` stays non-None, the ``published``
    # pointer does not move), so the draft is reviewable and ``request_publish_pocket``
    # can submit it. The canonical live Site doc and its URL are not claimed or
    # overwritten — only an approved review (the real ``publish``, ``preview=False``)
    # deploys live + promotes.
    if preview:
        from pocketpaw_ee.sites import local_server

        # DEP-3: map a missing-toolchain / non-zero generator failure to a clean
        # CloudError so the preview/edit/arm path never 500s opaquely either, BUT
        # leave SmokeGateFailed RAW (map_smoke_gate=False) so edit_svelte_component's
        # rollback-on-SmokeGateFailed contract is preserved on this preview path.
        build = await _build_or_cloud_error(
            generator,
            map_smoke_gate=False,
            ripple_spec=ripple_spec,
            theme=theme,
            site_id=site_id,
            title=site_name,
            capture_api_base=_capture_base(),
            capture_signed_key=signed_key,
            engine=engine,
            source=source,
            builder_origin=builder_origin,
            pocket_id=pocket_id,
            # A preview/edit/arm build skips only the SSR fail-gate (smoke=False); a
            # live publish keeps it (see _deploy_site_doc). It still BUILDS fresh +
            # anchored output either way.
            smoke=False,
        )

        # Serve at a STABLE per-pocket preview id (NOT the freshly-minted ObjectId)
        # so repeated preview builds overwrite the same dir and serve at the SAME
        # url — the builder iframe frames it once and just reloads. The transient
        # doc still carries the minted ObjectId in its ``id``/``script_name`` (it is
        # never persisted), but the served path + url use the stable preview id.
        preview_id = _preview_id(pocket_id)
        deploy = _local_deploy or local_server.deploy_local
        preview_url = deploy(preview_id, build.project_dir)
        return _SiteDoc(
            id=ObjectId(site_id),
            workspace=workspace_id,
            pocket_id=pocket_id,
            owner=user_id,
            name=site_name,
            script_name=site_id,
            deployed=False,  # a preview is NOT a live deploy
            signed_key=signed_key,
            url=preview_url,
            # Editable preview carries the bridge origin so the iframe can edit it.
            builder_origin=builder_origin or "",
            allowed_origins=_default_allowed_origins(),
            event_mapping=_DEFAULT_EVENT_MAPPING,
        )

    # BP-2 / #1345: promote the pocket's current draft version to ``published``
    # BEFORE deploy. ``_deploy_site_doc`` runs the smoke gate, so the version being
    # published is known-good; the published pointer is the durable "this is what
    # was published" record even if the deploy below fails (published != live — a
    # failed deploy just leaves the Site doc un-persisted, so the pocket reads
    # not-live while the published tag stands). The snapshot for the promote is the
    # engine's content: the rippleSpec for a ripple site, the {path: contents}
    # source map for a svelte site.
    version_content: dict[str, Any] = (source if is_source_engine(engine) else ripple_spec) or {}
    await _promote_pocket_draft_to_published(
        pocket_id=pocket_id,
        workspace_id=workspace_id,
        author=user_id,
        content=version_content,
    )

    # Generate + deploy + upsert the live Site doc. Extracted into a reusable helper
    # so the charge-first webhook (``activate_site``) can run the SAME deferred
    # deploy at payment-confirm time. ``publish`` (free path) calls it now.
    return await _deploy_site_doc(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key=signed_key,
        site_name=site_name,
        ripple_spec=ripple_spec,
        theme=theme,
        engine=engine,
        source=source,
        pattern=pattern,
        builder_origin=builder_origin,
        generator=generator,
        cloudflare=_cloudflare,
        bundle_reader=_bundle_reader,
        local_deploy=_local_deploy,
        workers_deploy=_workers_deploy,
    )


async def _deploy_site_doc(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    site_id: str,
    signed_key: str,
    site_name: str,
    ripple_spec: dict[str, Any] | None,
    theme: dict[str, Any],
    engine: str = "ripple",
    source: dict[str, str] | None = None,
    pattern: str | None = None,
    builder_origin: str | None = None,
    generator: GeneratorClient | None = None,
    cloudflare: Any | None = None,
    bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    local_deploy: Callable[[str, str], str] | None = None,
    workers_deploy: Callable[[str, str], Any] | None = None,
) -> _SiteDoc:
    """Generate, smoke-gate, deploy, and UPSERT the LIVE canonical Site doc.

    The deploy half of ``publish`` — extracted so the charge-first webhook
    (``activate_site``) can run the SAME deferred deploy at payment-confirm time
    using the inputs captured on the pending Site doc. It runs
    ``generator.build`` (with the SSR smoke fail-gate ON — a broken site is
    rejected before it deploys), deploys via one of THREE targets, and upserts ONE
    canonical Site doc per (workspace, pocket_id) keyed on the stable ``_id`` (==
    ``site_id``), flipping ``deployed=True`` + stamping ``deployed_at``.

    Deploy target (``_deploy_mode()`` reading PAW_CF_DEPLOY_MODE):
      * ``local``   → the LOCAL static server (``local_server.deploy_local``);
      * ``workers`` → a regular Worker on the free workers.dev tier
                      (``workers_deploy.deploy_workers``) — STATIC sites only; a
                      DYNAMIC site raises ``ValidationError`` (Phase 2 / use WfP);
      * ``wfp``     → Cloudflare Workers-for-Platforms (``cf.put_worker``).
    When PAW_CF_DEPLOY_MODE is UNSET the legacy selection stands: ``_local_mode()``
    → local, else WfP. An injected CF client forces the WfP branch over an
    env-requested local mode (the existing CF-test contract).

    Identity (``site_id`` / ``signed_key`` / ``site_name``) is resolved by the
    caller so the same values flow through a publish and a later activation (the
    deploy folder / URL / CF worker / Site doc id stay stable). On an upsert it
    PRESERVES domain/allowed_origins/signed_key and CLEARS any
    ``pending_deploy_inputs`` (the site is now live, not pending). Raises
    ``SmokeGateFailed`` (from generator_client) if the SSR render fails — nothing
    is deployed and no doc is flipped to deployed.

    ``workers_deploy`` is the test-injection seam for the workers-mode deployer
    (mirrors ``local_deploy``/``cloudflare``); ``None`` uses the real
    ``workers_deploy.deploy_workers``.

    DP0-4 — DYNAMIC split: a DYNAMIC site (``pattern == "dynamic"`` / a spec carrying
    live bindings) is NOT built or deployed inline here. Its per-tenant D1 data plane
    must be stood up by the durable ``provision_site`` job (create D1 → build with the
    real id → migrate → deploy), so this returns EARLY into ``_provision_dynamic_site``
    — which ensures the canonical Site doc in ``provision_status="provisioning"`` and
    enqueues the job (single-flight: a re-publish while already provisioning does NOT
    enqueue a second job). Only the STATIC path below runs the inline build → deploy →
    upsert (byte-for-byte the pre-DP0-4 behaviour, the regression guarantee).
    """
    # DP0-4: fork BEFORE any build. A dynamic site defers to the provision job; only
    # a static site takes the inline build/deploy/upsert path unchanged below.
    if _is_dynamic(pattern, ripple_spec):
        return await _provision_dynamic_site(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            site_id=site_id,
            signed_key=signed_key,
            site_name=site_name,
            builder_origin=builder_origin,
        )

    gen = generator or GeneratorClient()
    # DEP-3: map a generator/install/smoke failure (missing toolchain, non-zero
    # build, SSR fail-gate) to a clean CloudError (sites.generator_failed → 5xx)
    # instead of letting a bare FileNotFoundError / RuntimeError / SmokeGateFailed
    # escape as an unhandled 500. A misconfigured image (no paw-sites-gen / bun on
    # PATH) is the bug this guards.
    build = await _build_or_cloud_error(
        gen,
        ripple_spec=ripple_spec,
        theme=theme,
        site_id=site_id,
        title=site_name,
        capture_api_base=_capture_base(),
        capture_signed_key=signed_key,
        engine=engine,
        source=source,
        builder_origin=builder_origin,
        # PERF-3: build into the STABLE per-pocket working dir so node_modules
        # persists and `bun install` is cached across builds, cutting the dominant
        # per-edit cost.
        pocket_id=pocket_id,
        # A live deploy keeps the SSR fail-gate (smoke=True) so the gate + the
        # rollback in edit_svelte_component are unchanged — a broken edit never
        # reaches the live deploy.
        smoke=True,
    )

    # DS-2: a DYNAMIC site (pattern == "dynamic", or a spec carrying live
    # bindings) is backed by a per-tenant Cloudflare D1, so its deployed Worker
    # needs a D1 binding to reach that DB. Resolve the site's D1 id BEFORE deploy:
    # reuse the id already stored on this pocket's canonical Site doc (the binding
    # target must be stable across re-publishes), else derive a stable one. Static
    # sites get no D1 id and no binding — the single-module upload is unchanged.
    is_dynamic = _is_dynamic(pattern, ripple_spec)
    d1_database_id = ""
    if is_dynamic:
        _prior = await _SiteDoc.find_one({"_id": ObjectId(site_id), "workspace": workspace_id})
        d1_database_id = (
            getattr(_prior, "d1_database_id", "") if _prior is not None else ""
        ) or _derive_d1_database_id(workspace_id, pocket_id)

    # Deploy-mode selection (workers-deploy-mode). PAW_CF_DEPLOY_MODE explicitly
    # picks one of three targets (local | workers | wfp); when UNSET we preserve the
    # exact prior behaviour — ``_local_mode()`` → the local branch, else the
    # cf/put_worker (WfP) path. An injected CF client (tests) still FORCES the real
    # Cloudflare branch over an env-driven local selection, so the existing CF tests
    # keep exercising put_worker and the local branch never hijacks them.
    mode = _deploy_mode()
    if mode is None:
        # Legacy selection: local only when no CF client was injected AND the env
        # selects it; else WfP.
        use_local = cloudflare is None and _local_mode()
        mode = "local" if use_local else "wfp"
    elif mode == "local" and cloudflare is not None:
        # An injected CF client (a test asserting the real CF branch) wins over an
        # env that requests local — mirrors the legacy ``cloudflare is None`` guard.
        mode = "wfp"

    url = ""
    if mode == "local":
        from pocketpaw_ee.sites import local_server

        deploy = local_deploy or local_server.deploy_local
        url = deploy(site_id, build.project_dir)
    elif mode == "workers":
        # Free workers.dev tier — STATIC sites only. A dynamic site needs a
        # per-tenant D1 + Queues (Phase 2 / use WfP), so reject it cleanly rather
        # than deploying a broken site that can't reach its data.
        if is_dynamic:
            raise ValidationError(
                "sites.workers_dynamic_unsupported",
                "Dynamic sites aren't supported in workers mode yet (Phase 2) — they "
                "need a per-tenant D1; publish via Workers-for-Platforms instead.",
            )
        from pocketpaw_ee.sites import workers_deploy as workers_deploy_mod

        deploy_w = workers_deploy or workers_deploy_mod.deploy_workers
        url = await deploy_w(site_id, build.project_dir)
    else:  # "wfp"
        cf = cloudflare or _cf_client()
        bundle = bundle_reader(build.project_dir)
        # Only a dynamic site passes bindings; a static publish passes None so the
        # single-module upload path stays byte-for-byte unchanged (no regress).
        bindings = (
            [{"type": "d1", "name": _D1_BINDING_NAME, "id": d1_database_id}] if is_dynamic else None
        )
        await cf.put_worker(script_name=site_id, bundle=bundle, bindings=bindings)
        # CF-DISPATCH: the worker is now in the `paw-sites` dispatch namespace, but
        # a user worker in a WfP dispatch namespace is NOT directly URL-addressable
        # — it only serves when the dispatch worker
        # (ee/pocketpaw_ee/sites/cloudflare/dispatch-worker) routes
        # `<site_id>.<PAW_CF_SITES_DOMAIN>` to it. So the public URL is the
        # per-site subdomain, NOT a *.workers.dev address. When PAW_CF_SITES_DOMAIN
        # is configured, stamp it; when it is not, leave url="" (the deploy still
        # succeeded — the worker is uploaded — it is just unreachable until the
        # operator deploys the dispatch worker + sets the domain) and warn.
        import os

        domain = os.environ.get("PAW_CF_SITES_DOMAIN", "").strip()
        if domain:
            url = f"https://{site_id}.{domain}"
        else:
            logger.warning(
                "PAW_CF_SITES_DOMAIN unset — published site has no public URL "
                "(set it + deploy the dispatch worker)"
            )

    # PERF-1: UPSERT ONE canonical Site doc per (workspace, pocket_id) keyed on the
    # stable ``_id`` (== site_id), rather than inserting a fresh row every publish.
    # The stable id means the existing doc (if any) is found by ``_id`` directly; we
    # refresh the deploy-facing fields in place (a re-publish ships fresh content at
    # the same URL/worker). Fields a domain connect mutates later (``domains``,
    # ``allowed_origins``) are PRESERVED on update so connecting a domain survives a
    # re-publish; only a first insert seeds the defaults. ``signed_key`` is likewise
    # kept stable across re-publishes (the capture endpoint verifies against it).
    oid = ObjectId(site_id)
    # P2b: this runs ONLY on a successful deploy, so ``deployed`` flips True HERE —
    # stamp the live-deploy time alongside it. A re-publish refreshes it (it is
    # "last shipped", not "first shipped"). It is NOT a plain updatedAt bump, so it
    # stays a true "last deployed" marker.
    now = datetime.now(UTC)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        doc = _SiteDoc(
            id=oid,
            workspace=workspace_id,
            pocket_id=pocket_id,
            owner=user_id,
            name=site_name,
            script_name=site_id,
            deployed=True,
            deployed_at=now,
            signed_key=signed_key,
            url=url,
            # DS-2: persist the D1 id this dynamic site is bound to ("" for static)
            # so a re-publish reuses the SAME binding target and DS-3 can read it.
            d1_database_id=d1_database_id,
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
    else:
        doc.pocket_id = pocket_id
        doc.owner = user_id
        doc.name = site_name
        doc.script_name = site_id
        doc.deployed = True
        doc.deployed_at = now
        doc.url = url
        # charge-first: a live deploy clears any captured pending inputs — the site
        # is no longer pending payment, so the snapshot is no longer needed.
        doc.pending_deploy_inputs = {}
        # DS-2: keep the D1 id in sync. For a dynamic site it is the (reused)
        # stable id; a static re-publish leaves it "" (no binding). We only ever
        # SET it for a dynamic publish — a publish that is no longer dynamic does
        # not clear a previously-bound D1 (the data behind it must not be orphaned
        # silently), so guard on is_dynamic.
        if is_dynamic:
            doc.d1_database_id = d1_database_id
        await doc.save()
    return doc


async def _provision_dynamic_site(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    site_id: str,
    signed_key: str,
    site_name: str,
    builder_origin: str | None,
) -> _SiteDoc:
    """DP0-4 — the DYNAMIC half of ``_deploy_site_doc``: stand the site up
    ASYNCHRONOUSLY via the durable ``provision_site`` job instead of deploying inline.

    A dynamic site is backed by a per-tenant Cloudflare D1 that must be created,
    migrated, and bound BEFORE the Worker can serve — work the ``provision_site`` job
    owns (create D1 → build with the real id → migrate → deploy → mark provisioned).
    So rather than build/deploy here, this:

      1. ENSURES the ONE canonical Site doc for ``(workspace, pocket_id)`` exists in
         ``provision_status="provisioning"`` (``deployed=False``, ``url=""`` — the job
         flips those on finalize), mirroring the fields the static upsert seeds on a
         first publish (script_name / owner / signed_key / capture defaults).
      2. SINGLE-FLIGHT: if the doc is ALREADY ``provisioning``, it does NOT enqueue a
         second job — a double-publish is a no-op that returns the in-progress
         provisioning response (without a fresh job id).
      3. DISPATCHES the ``provision_site`` job (``params={}`` — the job re-reads the
         pocket's spec itself; ``validate_job_params({})`` passes with no schema).

    Returns the Site doc in ``provisioning`` state, carrying the enqueued job id on the
    transient ``_provision_job_id`` PrivateAttr so ``_to_response`` surfaces it. Both
    publish entry points (the free ``publish`` and the charge-first ``activate_site``)
    reach this through ``_deploy_site_doc``, so both get the async behaviour; each does
    its own post-processing on the returned doc (plan stamp / sub-active), which is a
    plain ``_SiteDoc`` exactly as before — only ``deployed``/``provision_status`` differ.
    """
    oid = ObjectId(site_id)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})

    # SINGLE-FLIGHT: a publish while the site is already provisioning must not enqueue
    # a second job — return the in-progress provisioning response as a no-op. (We do
    # not resolve the in-flight job id here; the caller can poll the site's status.)
    if doc is not None and doc.provision_status == "provisioning":
        doc._provision_job_id = None
        return doc

    # Ensure the canonical doc exists in ``provisioning`` state. leave ``deployed`` /
    # ``url`` for the job to finalize; seed the same identity/capture fields the static
    # upsert seeds so the site is coherent while it provisions.
    if doc is None:
        doc = _SiteDoc(
            id=oid,
            workspace=workspace_id,
            pocket_id=pocket_id,
            owner=user_id,
            name=site_name,
            script_name=site_id,
            deployed=False,
            url="",
            signed_key=signed_key,
            provision_status="provisioning",
            builder_origin=builder_origin or "",
            allowed_origins=_default_allowed_origins(),
            event_mapping=_DEFAULT_EVENT_MAPPING,
        )
        await doc.insert()
    else:
        # Re-publish of a not-currently-provisioning dynamic site (e.g. a failed /
        # provisioned one re-run): reset to ``provisioning`` and refresh the identity
        # fields. Preserve the stored ``signed_key`` (the capture endpoint verifies
        # against it) and any connected domain / allowlist. Clear any captured
        # pending-deploy inputs — the charge-first webhook has already handed off to
        # the job, so the snapshot is no longer needed.
        doc.pocket_id = pocket_id
        doc.owner = user_id
        doc.name = site_name
        doc.script_name = site_id
        doc.deployed = False
        doc.url = ""
        doc.provision_status = "provisioning"
        doc.pending_deploy_inputs = {}
        await doc.save()

    # DISPATCH the durable provision job. Lazy import keeps the sites service free of
    # an eager jobs/arq import at module load (mirrors the billing/pockets lazy
    # imports). ``params={}`` — the job re-reads the pocket spec; no params needed.
    from pocketpaw_ee.cloud.jobs import service as jobs_service

    result = await jobs_service.dispatch_job(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        action="provision_site",
        job_name="provision_site",
        params={},
        triggered_by=user_id,
    )
    doc._provision_job_id = result.get("job_id")
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


async def mark_site_subscription(
    *,
    workspace_id: str,
    site_id: str,
    status: str,
    subscription_id: str | None = None,
    annual_renewal_date: datetime | None = None,
) -> _SiteDoc | None:
    """Advance a site's per-site annual subscription lifecycle (BC-9).

    The entity-isolation write the billing webhook calls when a verified PER-SITE
    ``subscription.*`` delivery (one carrying a ``site_id``) lands: billing owns
    the Workspace / credits writes, the sites service owns the Site write, so the
    per-site sub state is updated HERE, not by billing reaching into the Site
    model. Sets ``subscription_status`` (active | cancelled), and on an
    activation/renewal also advances ``annual_renewal_date``. Reuses the stored
    ``subscription_id`` when the caller passes one (a renewal may re-confirm it).

    Tenant-scoped on ``workspace`` via ``_load`` (a missing / cross-tenant /
    malformed site id raises NotFound → the webhook acks without a write). Returns
    the updated doc, or raises NotFound when the site does not exist for the
    workspace (the caller — a verified webhook — treats that as nothing to do)."""
    site = await _load(workspace_id, site_id)
    site.subscription_status = status
    if subscription_id:
        site.subscription_id = subscription_id
    if annual_renewal_date is not None:
        site.annual_renewal_date = annual_renewal_date
    await site.save()
    return site


async def _canonical_site_doc(workspace_id: str, pocket_id: str) -> _SiteDoc | None:
    """The ONE canonical Site doc for (workspace, pocket_id), or None (PERF-1).

    Stable identity (``_live_object_id``) means a pocket published after PERF-1 has
    exactly one doc — the stable-id one. But PERF-1 does NOT migrate the dupes the
    old per-publish minting left behind (PERF-2 does), so a pocket may still have
    several Site docs, one of which the old arbitrary ``find_one`` could return with
    a stale ``url`` (the stale-live-link bug). This resolves the canonical doc
    deterministically:

      1. the STABLE-id doc (``_live_object_id``) when it exists — every post-PERF-1
         publish writes here, so it is the live one;
      2. otherwise the newest doc (by ``createdAt``) that actually carries a url —
         the freshest live build among legacy dupes;
      3. otherwise the newest doc at all (so a pre-url-era doc still resolves).

    Tenant-scoped on ``workspace``. Returns None when the pocket has no Site doc.
    """
    stable = await _SiteDoc.find_one(
        {"_id": _live_object_id(workspace_id, pocket_id), "workspace": workspace_id}
    )
    if stable is not None:
        return stable
    docs = (
        await _SiteDoc.find({"workspace": workspace_id, "pocket_id": pocket_id})
        .sort(-_SiteDoc.createdAt)  # type: ignore[operator]
        .to_list()
    )
    if not docs:
        return None
    # Prefer the newest doc that carries a real url (the freshest live build);
    # fall back to the newest doc overall when none has one.
    return next((d for d in docs if d.url), docs[0])


# ---------------------------------------------------------------------------
# DP0-3 — durable ``provision_site`` job seams.
#
# The provision job (ee/pocketpaw_ee/cloud/jobs/builtin/provision_site.py) takes a
# dynamic site from spec to live: create D1 → build (with the real id baked in) →
# apply migration → deploy the Worker → mark provisioned. The job orchestrates the
# steps (migrate sits BETWEEN build and deploy), so these are granular seams — the
# Site-doc Beanie reads/writes funnel through THIS module (the import-linter
# boundary keeps the builtin job off the Beanie doc), and the build/deploy
# mechanics reuse ``_deploy_site_doc``'s exact helpers rather than duplicating them.
# ---------------------------------------------------------------------------


def provision_cf_client() -> Any:
    """The Cloudflare client the provision job uses (create_database / put_worker).

    Thin public wrapper over ``_cf_client()`` so the builtin job builds the real CF
    client the SAME way ``_deploy_site_doc`` does, and tests can monkeypatch this one
    seam to inject a fake client without importing the client class."""
    return _cf_client()


def provision_d1_bindings(d1_database_id: str) -> list[dict]:
    """The Worker D1 binding list a provisioned dynamic site deploys with — a single
    ``{"type": "d1", "name": "DB", "id": <database_id>}`` (``_D1_BINDING_NAME``), the
    exact shape ``_deploy_site_doc`` passes ``put_worker`` on the dynamic path."""
    return [{"type": "d1", "name": _D1_BINDING_NAME, "id": d1_database_id}]


async def provision_deploy(
    *,
    site_id: str,
    project_dir: str,
    bundle: bytes,
    d1_database_id: str,
    cloudflare: Any = None,
) -> str:
    """Deploy a PROVISIONED dynamic site to whichever target ``_deploy_mode()`` names,
    and return its public URL. The one seam the provision job deploys through.

    Before this existed the job always called ``cf.put_worker``, which only uploads
    into a Workers-for-Platforms dispatch namespace. WfP is a PAID add-on, so an
    account without it got a bare ``Cloudflare API 403`` (CF error 10121) and NO
    dynamic site could ever publish — regardless of PAW_CF_DEPLOY_MODE, which the job
    never consulted. Honouring the mode here gives dynamic sites the same free
    workers.dev target static sites already use:

      * ``workers`` → ``workers_deploy.deploy_workers`` with the D1 bound as ``DB``.
        Free tier, no dispatch namespace. Returns the real workers.dev URL.
      * ``wfp`` / UNSET → the pre-existing ``put_worker`` upload (unchanged), whose
        URL comes from ``provision_site_url``.

    ``local`` is not a dynamic target (nothing serves the D1 binding locally), so it
    degrades to ``workers`` rather than deploying a site that cannot reach its own
    database."""
    mode = _deploy_mode()
    if mode == "local":
        logger.info("sites.provision: local mode has no dynamic target — using workers mode")
        mode = "workers"

    if mode == "workers":
        from pocketpaw_ee.sites import workers_deploy as workers_deploy_mod

        return await workers_deploy_mod.deploy_workers(
            site_id, project_dir, d1_database_id=d1_database_id
        )

    cf = cloudflare or _cf_client()
    await cf.put_worker(
        script_name=site_id,
        bundle=bundle,
        bindings=provision_d1_bindings(d1_database_id),
    )
    return provision_site_url(site_id)


def provision_site_url(site_id: str) -> str:
    """The public URL a provisioned dynamic site resolves to — the per-site subdomain
    ``https://<site_id>.<PAW_CF_SITES_DOMAIN>`` when the sites domain is configured,
    else "" (the Worker is uploaded + reachable via the dispatch worker once the
    operator sets the domain). Mirrors ``_deploy_site_doc``'s WfP URL logic."""
    import os

    domain = os.environ.get("PAW_CF_SITES_DOMAIN", "").strip()
    return f"https://{site_id}.{domain}" if domain else ""


async def load_provision_site(workspace_id: str, pocket_id: str) -> _SiteDoc | None:
    """The canonical Site doc the provision job operates on, or None. Resolves the
    ONE canonical doc for (workspace, pocket_id) (``_canonical_site_doc``), so a
    pre-PERF-1 pocket with legacy dupes still provisions the live one."""
    return await _canonical_site_doc(workspace_id, pocket_id)


async def persist_provision_d1_id(site: _SiteDoc, d1_database_id: str) -> None:
    """Persist a freshly-created D1 id on the Site doc IMMEDIATELY (DP0-3 contract).

    ``provision_status`` stays ``provisioning`` — only ``d1_database_id`` is stamped
    — so a retry of the job REUSES this D1 instead of creating (and orphaning) a
    second one. Called right after ``cf.create_database`` succeeds, before build /
    migrate / deploy."""
    site.d1_database_id = d1_database_id
    site.provision_status = "provisioning"
    await site.save()


async def finalize_provisioned_site(site: _SiteDoc, *, url: str) -> None:
    """Mark a Site doc fully provisioned after migrate + deploy succeed (DP0-3):
    ``provision_status="provisioned"``, ``deployed=True``, stamp the live URL +
    ``deployed_at``. The last write of the durable job's happy path."""
    site.provision_status = "provisioned"
    site.deployed = True
    site.deployed_at = datetime.now(UTC)
    site.url = url
    await site.save()


async def mark_provision_failed(site: _SiteDoc) -> None:
    """Mark a Site doc's provision as failed (DP0-3). The ``d1_database_id`` already
    persisted (``persist_provision_d1_id``) is LEFT in place so a retry reuses that
    D1 — only ``provision_status`` flips to ``failed``."""
    site.provision_status = "failed"
    await site.save()


async def build_provision_bundle(
    *,
    site: _SiteDoc,
    ripple_spec: dict[str, Any] | None,
    d1_database_id: str,
    generator: GeneratorClient | None = None,
) -> tuple[str, bytes]:
    """Build a dynamic site with its REAL D1 id baked in; return ``(project_dir,
    bundle)`` (DP0-3).

    Reuses ``_deploy_site_doc``'s exact build assembly: it derives the theme from
    the rippleSpec, runs the SSR-gated build through ``_build_or_cloud_error`` (so a
    broken build maps to a clean CloudError, never an opaque 500), and reads the
    Worker bundle via ``_default_bundle_reader``. ``d1_database_id`` rides
    ``siteConfig.d1DatabaseId`` so the generator threads it into the emitted
    wrangler.toml ``database_id`` — this is what makes the later ``wrangler d1
    migrations apply`` and the Worker's D1 binding target the right database. The
    caller applies migrations against ``project_dir`` (which carries wrangler.toml +
    migrations/) BEFORE deploying ``bundle``. ``generator`` is the test-injection
    seam; None uses a real ``GeneratorClient``."""
    gen = generator or GeneratorClient()
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    site_id = str(site.id)
    build = await _build_or_cloud_error(
        gen,
        ripple_spec=ripple_spec,
        theme=theme,
        site_id=site_id,
        title=site.name or "Untitled site",
        capture_api_base=_capture_base(),
        capture_signed_key=site.signed_key,
        # A dynamic Paw Site is authored as a ripple site carrying live bindings
        # (objects / sources / actions / auth) — the create-dynamic-site path.
        engine="ripple",
        source=None,
        builder_origin=site.builder_origin or None,
        # DP0-3: the REAL D1 uuid, baked into the emitted wrangler.toml.
        d1_database_id=d1_database_id,
        pocket_id=site.pocket_id,
        # A live provision keeps the SSR fail-gate on — a broken site is rejected
        # before it deploys (same as _deploy_site_doc).
        smoke=True,
    )
    bundle = _default_bundle_reader(build.project_dir)
    return build.project_dir, bundle


async def add_domain(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> DomainStatusResponse:
    """Register a custom hostname with Cloudflare for SaaS and store it on the
    site. Returns the ONE CNAME the client pastes at their registrar."""
    # BC-10: a higher site-plan tier resells Cloudflare paid features (WAF /
    # edge-cache / strict TLS). Lazy import mirrors publish_pocket's site_plans
    # use — keeps the billing catalog off the module-import path.
    from pocketpaw_ee.cloud.billing import site_plans

    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)
    # Resolve the site's tier → its cloudflare_features and provision them on the
    # custom hostname. A base-tier (or unknown) site resolves to an empty set, so
    # create_custom_hostname stays on the basic path.
    plan = site_plans.get_site_plan(site.plan_tier)
    features = set(plan.cloudflare_features) if plan else set()
    ch = await cf.create_custom_hostname(hostname, features=features)
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
    site_plan_key: str | None = None,
    builder_origin: str | None = None,
    preview: bool = False,
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
    _billing_provider: Any | None = None,
) -> _SiteDoc:
    """Publish a pocket as a site by id — the shared path for the REST router
    and the in-process MCP tool.

    BC-9 (per-site annual plan — the Webflow model): a published site carries its
    OWN recurring annual plan on a tier. ``site_plan_key`` selects the tier from
    the site-plan catalog (``billing.site_plans``); it defaults to the base tier.
    After a LIVE publish (not a preview) inserts the Site doc, this:
      * GATES on the workspace's Sites entitlement (already enforced first via
        ``publish`` → ``require_sites_plan`` — a workspace lacking it never reaches
        the Site insert);
      * stamps ``Site.plan_tier`` + ``Site.subscription_id`` for the tier;
      * initiates a PER-SITE annual Dodo subscription (BC-7's ``create_subscription``)
        with ``metadata={workspace_id, site_id, plan_key}`` — the ``site_id`` is how
        the renewal webhook tells a per-site sub from a workspace-plan sub. When
        Dodo is not configured (no recurring product for the site tier), this
        DEGRADES gracefully: it records the intended tier without a live charge
        (``subscription_id`` stays None), never crashing the publish;
      * emits ``SitePublished`` ({workspace_id, site_id, pocket_id, owner,
        plan_tier}).
    A preview publish skips all of this (it never persists a Site doc).

    ``preview`` (Branch primitive — EDIT/arm path) is forwarded straight to
    ``publish``: ``True`` builds + smoke-gates + locally serves a DRAFT preview
    WITHOUT promoting the draft to published or claiming the live deploy (it
    returns a transient preview Site doc); ``False`` (the default) is a real live
    publish + promote.

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
    # Plan gate FIRST — before reading the pocket — so a team-plan caller gets
    # plan.feature_denied regardless of whether the pocket exists, rather than a
    # misleading pocket.not_found. ``publish`` re-checks for direct callers; the
    # repeat read is a single cheap workspace lookup.
    await require_sites_plan(workspace_id)

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
    # DS-2: the pocket's create-pocket layout pattern. ``pattern == "dynamic"``
    # (stamped by the create-dynamic-site tool) tells ``publish`` the site is
    # backed by a per-tenant D1, so its deployed Worker gets a D1 binding.
    pattern = pocket.get("pattern")

    # charge-first: a PREVIEW publish never persists a Site doc and never bills, so
    # it stays the unchanged Branch-primitive preview path — build + smoke-gate +
    # locally serve a transient draft, no tier/sub/event.
    if preview:
        return await publish(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            ripple_spec=ripple_spec,
            theme=theme,
            engine=engine,
            source=source,
            pattern=pattern,
            name=name or pocket.get("name", ""),
            builder_origin=builder_origin,
            preview=True,
            _generator=_generator,
            _cloudflare=_cloudflare,
            _bundle_reader=_bundle_reader,
            _local_deploy=_local_deploy,
        )

    # charge-first: resolve the per-site tier BEFORE deploy, then branch.
    #   * a PAID tier (positive annual price AND a configured Dodo product) DEFERS
    #     the live deploy — create the Site as PENDING, open the annual checkout,
    #     return the checkout_url, and let the ``subscription.active`` webhook deploy
    #     it live once payment is confirmed (``_publish_pending_site``).
    #   * a FREE/base tier — OR a "paid" tier whose Dodo product is NOT configured
    #     (a paid tier can't open a checkout, so don't strand the user) — publishes
    #     LIVE immediately, exactly as before, then stamps the (free/degraded) tier.
    from pocketpaw_ee.cloud.billing import site_plans

    tier = site_plans.get_site_plan(site_plan_key) or site_plans.get_site_plan(
        site_plans.BASE_SITE_PLAN_KEY
    )
    is_paid = tier is not None and tier.annual_price_usd > 0
    dodo_configured = tier is not None and bool(tier.dodo_product_id)

    if is_paid and not dodo_configured:
        # A paid tier whose Dodo product is unconfigured can't open a checkout —
        # fall back to publishing LIVE immediately so the user is never stranded.
        logger.warning(
            "sites.publish: paid tier %s has no configured Dodo product — publishing "
            "live immediately (charge-first fallback, no checkout)",
            tier.key if tier is not None else site_plan_key,
        )

    if is_paid and dodo_configured:
        # PAID + chargeable → defer the deploy until subscription.active.
        return await _publish_pending_site(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            ripple_spec=ripple_spec,
            theme=theme,
            engine=engine,
            source=source,
            pattern=pattern,
            name=name or pocket.get("name", ""),
            builder_origin=builder_origin,
            tier=tier,
            provider=_billing_provider,
        )

    # FREE/base (or paid-but-unconfigured fallback) → publish LIVE now.
    doc = await publish(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        ripple_spec=ripple_spec,
        theme=theme,
        engine=engine,
        source=source,
        pattern=pattern,
        name=name or pocket.get("name", ""),
        builder_origin=builder_origin,
        preview=False,
        _generator=_generator,
        _cloudflare=_cloudflare,
        _bundle_reader=_bundle_reader,
        _local_deploy=_local_deploy,
    )

    # Stamp the per-site annual plan, open the per-site Dodo sub (degrading
    # gracefully when Dodo is unconfigured), and emit SitePublished. No checkout_url
    # on this path — the site is already live.
    return await _apply_site_plan(
        doc=doc,
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        site_plan_key=site_plan_key,
        provider=_billing_provider,
    )


async def _apply_site_plan(
    *,
    doc: _SiteDoc,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    site_plan_key: str | None,
    provider: Any | None,
) -> _SiteDoc:
    """Stamp the per-site annual plan on a freshly published Site doc, open its
    per-site Dodo subscription, and emit ``SitePublished`` (BC-9).

    The site tier resolves from the site-plan catalog (``billing.site_plans``);
    an unknown / missing key falls back to the base tier so a publish never fails
    on a typo'd tier (the gate that matters — Sites entitlement — already ran in
    ``publish``). The subscription is opened through BC-7's provider
    (``create_subscription``) with ``metadata={workspace_id, site_id, plan_key}``;
    the ``site_id`` is what the renewal webhook routes on to tell a per-site sub
    from a workspace-plan sub. When the tier has no configured Dodo recurring
    product (v1 default), this DEGRADES gracefully — it records the tier without a
    live charge (``subscription_id`` stays None) and never crashes the publish.

    Lazy imports (billing catalog / provider / emit) keep the sites service free
    of an eager billing import at module load."""
    from pocketpaw_ee.cloud._core.realtime.emit import emit
    from pocketpaw_ee.cloud._core.realtime.events import SitePublished
    from pocketpaw_ee.cloud.billing import site_plans

    # Resolve the tier; fall back to the base tier for a None / unknown key so a
    # publish is never blocked by a bad tier string (the entitlement gate is the
    # one that matters and already ran).
    tier = site_plans.get_site_plan(site_plan_key) or site_plans.get_site_plan(
        site_plans.BASE_SITE_PLAN_KEY
    )
    plan_key = tier.key if tier is not None else site_plans.BASE_SITE_PLAN_KEY

    site_id = str(doc.id)
    subscription_id: str | None = None
    # Open a PER-SITE annual Dodo subscription when the tier has a configured
    # recurring product. metadata.site_id is the per-site discriminator the
    # webhook routes on. No product configured (v1 default) → record the tier
    # without a live charge; never crash the publish.
    if tier is not None and tier.dodo_product_id:
        try:
            prov = provider or _default_billing_provider()
            checkout = await prov.create_subscription(
                plan_key=plan_key,
                product_id=tier.dodo_product_id,
                workspace_id=workspace_id,
                customer_email=None,
                metadata={
                    "workspace_id": workspace_id,
                    "site_id": site_id,
                    "plan_key": plan_key,
                },
            )
            subscription_id = checkout.subscription_id or None
        except Exception:  # noqa: BLE001 — a billing hiccup must not undo the deploy
            logger.warning(
                "sites.publish: per-site subscription init failed for site %s "
                "(tier=%s) — recording the tier without a live charge",
                site_id,
                plan_key,
                exc_info=True,
            )

    # Stamp the per-site plan on the (already persisted) canonical Site doc.
    doc.plan_tier = plan_key
    doc.subscription_id = subscription_id
    # A live sub starts pending until Dodo posts a verified subscription.active;
    # an unconfigured (no-charge) tier has no live sub to activate, so it stays
    # "none". The per-site webhook advances "pending" → "active".
    doc.subscription_status = "pending" if subscription_id else "none"
    await doc.save()

    await emit(
        SitePublished(
            data={
                "workspace_id": workspace_id,
                "site_id": site_id,
                "pocket_id": pocket_id,
                "owner": user_id,
                "plan_tier": plan_key,
            }
        )
    )
    return doc


async def _publish_pending_site(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    ripple_spec: dict[str, Any] | None,
    theme: dict[str, Any],
    engine: str,
    source: dict[str, str] | None,
    pattern: str | None,
    name: str,
    builder_origin: str | None,
    tier: Any,
    provider: Any | None,
) -> _SiteDoc:
    """Charge-first: create a PAID-tier site as PENDING and open its checkout,
    WITHOUT deploying it live.

    The paid-tier half of ``publish_pocket``. Rather than generate + deploy and
    then bill, it:
      1. resolves the STABLE per-(workspace, pocket) identity (the same ``site_id``
         a live publish would use, and the existing signed_key when the pocket was
         published before) so a later ``activate_site`` deploys at the same id/URL.
         The ``site_id`` is deterministic (``_live_object_id``), so it can be passed
         in the subscription metadata WITHOUT the Site doc existing yet;
      2. caps the serialized size of the captured DEPLOY INPUTS (review fix A) —
         a pathological rippleSpec / source map that would bloat the Site doc past
         ``_MAX_PENDING_DEPLOY_INPUT_BYTES`` raises ``ValidationError`` BEFORE
         anything is persisted or billed;
      3. opens the per-site annual Dodo subscription FIRST (review fix B —
         checkout-before-persist) with ``metadata={workspace_id, site_id, plan_key}``,
         so a checkout failure NEVER leaves an orphan pending Site doc with no
         ``subscription_id``: the error propagates and the user can retry, and no
         Site doc was written at all (or, on a re-publish, the prior doc is left
         untouched);
      4. only THEN upserts the PENDING canonical Site doc — ``deployed=False``,
         ``subscription_status="pending"``, ``plan_tier=<tier>``, ``subscription_id``
         already set — persisting the DEPLOY INPUTS (rippleSpec / theme / engine /
         source / pattern / builder_origin / name) on ``pending_deploy_inputs`` so
         the webhook-time activation can deploy WITHOUT re-reading the pocket (the
         webhook carries only workspace_id + site_id, and the pocket's draft may
         have moved on);
      5. stashes the checkout_url on the returned doc's transient ``_checkout_url``
         for the router to surface.

    It does NOT run the generator, does NOT deploy, does NOT promote the pocket's
    draft to published (the site is not live yet), and does NOT emit
    ``SitePublished`` — all of that happens in ``activate_site`` when the
    ``subscription.active`` webhook confirms payment.
    """
    site_name = (name or "").strip()
    if not site_name:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        _pocket = await pockets_service.get(pocket_id, user_id)
        site_name = (_pocket.get("name") or "").strip()
    if not site_name:
        site_name = "Untitled site"

    oid = _live_object_id(workspace_id, pocket_id)
    site_id = str(oid)
    plan_key = tier.key

    # The deploy inputs the webhook-time activation needs (the webhook carries only
    # workspace_id + site_id). Stored on the pending doc so activation never re-reads
    # the pocket (whose draft may have advanced).
    pending_inputs: dict[str, Any] = {
        "ripple_spec": ripple_spec,
        "theme": theme,
        "engine": engine,
        "source": source,
        "pattern": pattern,
        "builder_origin": builder_origin,
        "name": site_name,
    }

    # Review fix A — cap the serialized deploy-input size BEFORE any persist or
    # billing. A pathological rippleSpec / svelte source map must not bloat the
    # Site doc toward Mongo's 16MB ceiling. Fail closed with a clear 422.
    serialized_len = len(json.dumps(pending_inputs, default=str))
    if serialized_len > _MAX_PENDING_DEPLOY_INPUT_BYTES:
        raise ValidationError(
            "sites.deploy_inputs_too_large",
            (
                "This site's content is too large to publish on a paid plan "
                f"({serialized_len} bytes captured, max "
                f"{_MAX_PENDING_DEPLOY_INPUT_BYTES}). Trim large inlined data or "
                "images from the page and try again."
            ),
        )

    # Reuse the stored signed_key when the pocket was published before, so the
    # eventual deploy bakes a key that matches the doc the capture endpoint
    # verifies against; mint one for a first publish. Read the existing doc ONLY
    # for the signed_key here — we do NOT mutate or persist it yet (review fix B:
    # the checkout must open before any persist), so a checkout failure leaves a
    # previously-published doc untouched.
    signed_key = f"site_key_{secrets.token_urlsafe(24)}"
    existing = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if existing is not None and existing.signed_key:
        signed_key = existing.signed_key

    # Review fix B — open the per-site annual Dodo subscription FIRST, before
    # persisting the pending doc. The site_id is deterministic, so the checkout
    # metadata carries it without the doc existing yet. If opening the checkout
    # raises, it PROPAGATES (no swallow) and NO pending doc is created — never an
    # orphan pending row with no subscription_id. The buyer can simply retry.
    prov = provider or _default_billing_provider()
    checkout = await prov.create_subscription(
        plan_key=plan_key,
        product_id=tier.dodo_product_id,
        workspace_id=workspace_id,
        customer_email=None,
        metadata={
            "workspace_id": workspace_id,
            "site_id": site_id,
            "plan_key": plan_key,
        },
    )
    checkout_url: str | None = checkout.checkout_url or None
    subscription_id: str | None = checkout.subscription_id or None

    # Checkout opened — NOW upsert the PENDING canonical Site doc with the
    # subscription_id already set (review fix B). The size cap (A) already ran.
    if existing is None:
        doc = _SiteDoc(
            id=oid,
            workspace=workspace_id,
            pocket_id=pocket_id,
            owner=user_id,
            name=site_name,
            script_name=site_id,
            deployed=False,  # PENDING — not deployed until payment confirms
            signed_key=signed_key,
            url="",
            builder_origin=builder_origin or "",
            plan_tier=plan_key,
            subscription_id=subscription_id,
            subscription_status="pending",
            pending_deploy_inputs=pending_inputs,
            allowed_origins=_default_allowed_origins(),
            event_mapping=_DEFAULT_EVENT_MAPPING,
        )
        await doc.insert()
    else:
        # Re-publish of a pocket onto a paid tier: refresh the pending intent in
        # place. A previously-live site is taken back to pending until the new
        # annual sub confirms — but the deploy fields are LEFT as-is until the
        # activation re-deploys (we don't tear down a live site before payment).
        doc = existing
        doc.owner = user_id
        doc.name = site_name
        doc.plan_tier = plan_key
        doc.subscription_id = subscription_id
        doc.subscription_status = "pending"
        doc.pending_deploy_inputs = pending_inputs
        await doc.save()

    # Stash the checkout link on the transient PrivateAttr so the router can surface
    # it on SiteResponse.checkout_url (never persisted).
    doc._checkout_url = checkout_url
    return doc


async def activate_site(
    *,
    workspace_id: str,
    site_id: str,
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Deploy + activate a PENDING charge-first site (charge-first activation).

    Called from the per-site ``subscription.active`` webhook once payment is
    confirmed. Loads the PENDING Site doc (tenant-scoped via ``_load`` — a missing /
    cross-tenant id raises NotFound, which the webhook acks without a write), reads
    the DEPLOY INPUTS captured on it at publish time (``pending_deploy_inputs``),
    and runs the SAME deferred deploy a live publish would have (``_deploy_site_doc``
    — generate + smoke-gate + deploy + upsert ``deployed=True`` + stamp
    ``deployed_at``). It then marks ``subscription_status="active"``, promotes the
    pocket's draft to published (the durable "this was published" record, mirroring
    the live publish), and emits ``SitePublished``.

    Idempotent for the webhook's at-least-once delivery: an already-active /
    already-deployed site is returned unchanged (no re-deploy, no re-emit) — a
    replayed or out-of-order ``subscription.active`` is a no-op.

    If the pending doc carries NO ``pending_deploy_inputs`` (an unexpected state —
    e.g. a site that was never a charge-first pending publish), it cannot be
    deployed from the webhook; the status is still advanced to active and the doc
    returned, and the gap is logged. ``_generator`` / ``_cloudflare`` / etc. are
    injectable so the path is unit-testable without Bun / workerd / Cloudflare.
    """
    doc = await _load(workspace_id, site_id)

    # Idempotent: an already-deployed+active site is a no-op (replayed / out-of-order
    # delivery). We treat "deployed and active" as the terminal live state.
    if doc.deployed and doc.subscription_status == "active":
        return doc

    inputs = doc.pending_deploy_inputs or {}
    if not inputs:
        # No captured deploy inputs — we cannot run the deferred deploy from the
        # webhook (it carries no pocket scope). This is an unexpected/corrupt state:
        # every charge-first pending site captures its inputs at publish, and they
        # are only cleared on a SUCCESSFUL deploy. Do NOT advance to "active" — that
        # would be a lie (active + deployed=False = a paid site that 404s). Leave it
        # PENDING and log loudly so an operator investigates; the site never claims
        # to be live without an actual deploy.
        logger.error(
            "sites.activate: pending site %s has no captured deploy inputs — "
            "leaving it PENDING (cannot deploy from the webhook); operator "
            "intervention required",
            site_id,
        )
        return doc

    pocket_id = doc.pocket_id
    # Deploy live using the inputs captured at publish time (NOT a fresh pocket read
    # — the webhook has no pocket scope, and the pocket's draft may have advanced).
    deployed = await _deploy_site_doc(
        workspace_id=workspace_id,
        user_id=doc.owner,
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key=doc.signed_key,
        site_name=inputs.get("name") or doc.name,
        ripple_spec=inputs.get("ripple_spec"),
        theme=inputs.get("theme") or {},
        engine=inputs.get("engine") or "ripple",
        source=inputs.get("source"),
        pattern=inputs.get("pattern"),
        builder_origin=inputs.get("builder_origin"),
        generator=_generator,
        cloudflare=_cloudflare,
        bundle_reader=_bundle_reader,
        local_deploy=_local_deploy,
    )

    # The deploy flipped ``deployed=True`` and cleared pending_deploy_inputs; now
    # mark the per-site sub active and advance the annual renewal date (one year out
    # — the next charge cycle), mirroring the renewed path.
    deployed.subscription_status = "active"
    deployed.annual_renewal_date = datetime.now(UTC) + timedelta(days=365)
    await deployed.save()

    # Promote the pocket's draft to published — the durable "this was published"
    # record, mirroring the live publish path (best-effort; versioning never gates
    # the deploy/activation).
    version_content: dict[str, Any] = (
        inputs.get("source")
        if is_source_engine(inputs.get("engine"))
        else inputs.get("ripple_spec")
    ) or {}
    await _promote_pocket_draft_to_published(
        pocket_id=pocket_id,
        workspace_id=workspace_id,
        author=doc.owner,
        content=version_content,
    )

    # Emit SitePublished now that the site is actually live (deferred from the
    # pending publish — a charge-first site is "published" only once it deploys).
    from pocketpaw_ee.cloud._core.realtime.emit import emit
    from pocketpaw_ee.cloud._core.realtime.events import SitePublished

    await emit(
        SitePublished(
            data={
                "workspace_id": workspace_id,
                "site_id": site_id,
                "pocket_id": pocket_id,
                "owner": doc.owner,
                "plan_tier": deployed.plan_tier or "",
            }
        )
    )
    return deployed


def _default_billing_provider() -> Any:
    """Build the default payments provider (Dodo) for a per-site subscription.

    Lazy so importing the sites service never constructs an SDK client; tests
    inject their own provider via ``publish_pocket(_billing_provider=...)``."""
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider

    return DodoProvider.from_settings(get_settings())


def apply_edits(source: str, edits: list[dict[str, str]]) -> str:
    """Apply a list of search/replace blocks to ``source`` and return the result
    (P3 — the TARGETED / DIFF edit primitive). PURE + I/O-free, so it is directly
    unit-testable; ``edit_svelte_component`` calls it to turn the agent's minimal
    diff into the new file contents before reusing the unchanged persist path.

    Each block is ``{"old_string": <str>, "new_string": <str>}``. The contract
    mirrors the built-in Edit tool so the agent's existing instinct transfers:

      * ``old_string`` must match the CURRENT working text EXACTLY ONCE. 0 matches
        or >1 matches raise ``ValidationError`` with a clear, retry-able message
        (the agent makes ``old_string`` more specific and retries) — never a silent
        no-op or a partial/ambiguous replace.
      * blocks apply SEQUENTIALLY against the running result, so a later block can
        target text an earlier block produced.
      * ``new_string`` may be empty (a deletion); ``old_string == new_string`` is a
        no-op the agent did not intend and is rejected.

    Raises ``ValidationError`` on an empty list, a malformed block (missing/non-str
    keys), or any match-count violation — so a bad diff fails closed BEFORE
    anything is persisted or rebuilt.
    """
    if not isinstance(edits, list) or not edits:
        raise ValidationError(
            "site_edit.empty_edits",
            "edits must be a non-empty list of {old_string, new_string} blocks.",
        )
    result = source
    for i, block in enumerate(edits):
        if not isinstance(block, dict):
            raise ValidationError(
                "site_edit.malformed_block",
                f"edit block {i} is not an object with old_string/new_string.",
            )
        old = block.get("old_string")
        new = block.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValidationError(
                "site_edit.malformed_block",
                f"edit block {i} must have string `old_string` and `new_string`.",
            )
        if old == "":
            raise ValidationError(
                "site_edit.empty_old_string",
                f"edit block {i} has an empty `old_string` — provide the exact text to replace.",
            )
        if old == new:
            raise ValidationError(
                "site_edit.noop_block",
                f"edit block {i} has identical old_string and new_string (no-op) — "
                "the change would do nothing.",
            )
        count = result.count(old)
        if count == 0:
            raise ValidationError(
                "site_edit.no_match",
                f"edit block {i}: old_string was not found (0 times) in the current "
                "source — it must match the file exactly. Re-read the component and "
                "copy the text verbatim.",
            )
        if count > 1:
            raise ValidationError(
                "site_edit.ambiguous_match",
                f"edit block {i}: old_string matches {count} times — it must be "
                "unique. Include more surrounding context so it matches exactly once.",
            )
        result = result.replace(old, new, 1)
    return result


async def apply_leaf_edits(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    edits: list[dict[str, Any]],
    _apply: Any = None,
) -> list[dict[str, Any]]:
    """Persist native-editor leaf edits as a reviewable Branch draft (NE-4b).

    The backend of the native site-editing persist path. The native editor forwards
    a batch of ``{uid, op}`` leaf edits it has ALREADY rendered optimistically; this
    splices them into the pocket's svelte ``source`` map via the paw-sites
    ``apply-leaf-edit`` CLI (a PURE transform — no build, no workerd) and persists
    each CHANGED file through the pockets service, which auto-writes a Branch draft
    ArtifactVersion snapshotting the full edited source map. There is NO
    republish/rebuild: persisting the draft is the whole job — the deliberate UX win
    over ``edit_svelte_component``'s per-edit iframe rebuild (the editor already
    shows the change; an approved review is what later takes it live).

    Steps:
      1. reject an empty edit batch (``ValidationError`` → 422);
      2. read the pocket (the pockets service's public ``get`` raises NotFound /
         Forbidden itself — a missing / cross-tenant pocket is 404 / 403) and assert
         it is a svelte site (else ``ValidationError`` → 422);
      3. splice via the generator bridge — edits apply IN ORDER, and a rejected edit
         leaves ITS file byte-identical in the returned source;
      4. persist ONLY the files whose contents actually changed (so a rejected edit
         naturally persists nothing and never churns an empty draft), each write
         auto-writing the Branch draft snapshot;
      5. return the per-uid verdicts unchanged.

    ``_apply`` is an injectable seam for the generator bridge (defaults to
    ``generator_client.apply_leaf_edits``) so the orchestration is unit-testable
    without Bun / workerd.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import generator_client

    if not isinstance(edits, list) or not edits:
        raise ValidationError(
            "site_leaf_edit.empty_edits",
            "apply_leaf_edits requires a non-empty list of {uid, op} leaf edits.",
        )

    # The pockets service's PUBLIC get raises NotFound / Forbidden itself (entity
    # isolation) — a missing / cross-tenant pocket surfaces as 404 / 403.
    pocket = await pockets_service.get(pocket_id, user_id)
    # KEPT svelte-specific (not is_source_engine): the body below runs the DSV-5
    # ``_split_svelte_source`` split and the SvelteKit leaf-edit CLI, both svelte-only.
    # HE-9 widens THIS guard to html (via is_source_engine) in the same change that
    # teaches the CLI the html editing lane, so guard and body widen together.
    if (pocket.get("engine") or "ripple") != "svelte" or not isinstance(pocket.get("source"), dict):
        raise ValidationError(
            "pocket.not_svelte_site",
            "This pocket is not a svelte Paw Site — it has no component source map to edit.",
        )
    source_map = pocket["source"]

    # DSV-5 binding-key safety: a DYNAMIC svelte pocket's source envelope carries its
    # live-data bindings (objects/sources/actions/auth) as SIBLING keys of the
    # {path: contents} SvelteKit files. Split them out exactly like the build path
    # (_split_svelte_source): the leaf-edit CLI must receive ONLY the file map (it
    # treats every source key as a file, so a binding key mixed in would break the
    # splice), and the persist loop below is CONFINED to this input file keyspace so a
    # binding key is never written back as a component file and a brand-new key the
    # CLI might invent is never persisted.
    files, _bindings = generator_client._split_svelte_source(source_map)

    # Splice the {uid, op} edits into the file map via the apply-leaf-edit CLI. The
    # bridge raises a bare RuntimeError on a non-zero exit / timed-out splice, and the
    # result parse can KeyError / IndexError / TypeError on malformed CLI output — map
    # them all to a clean CloudError (sites.leaf_edit_failed → 5xx) so the client gets
    # a structured envelope with a reason instead of an opaque unhandled 500 (the
    # cloud error handler maps ONLY CloudError). A CloudError raised inside is
    # re-raised unchanged; the cause is chained for logs, never leaked to the client.
    try:
        out = await (_apply or generator_client.apply_leaf_edits)(source=files, edits=edits)
        new_map, results = out["source"], out["results"]
    except CloudError:
        raise
    except (RuntimeError, KeyError, IndexError, TypeError) as exc:
        logger.error("sites.apply_leaf_edits: leaf-edit bridge failed", exc_info=True)
        raise with_cause(
            Internal(
                "sites.leaf_edit_failed",
                "Applying the edits failed — the editing toolchain is unavailable or "
                "returned an unexpected result. See server logs for details.",
            ),
            exc,
        ) from exc

    # Persist ONLY the files that actually changed, CONFINED to the input file
    # keyspace. A rejected edit leaves its file byte-identical (the CLI contract), so
    # this comparison naturally skips it — no empty draft churn. A binding key or a
    # brand-new path the CLI echoed / invented is NOT in ``files``, so it is skipped
    # too (never persisted as a component file — set_svelte_source_file would
    # otherwise overwrite a live-data binding or 404 on an unknown path). Each write
    # auto-writes a Branch draft snapshotting the FULL edited map, so after the loop
    # the draft == the fully edited source. Multi-file safe.
    for path, contents in new_map.items():
        if path not in files:
            continue
        if files.get(path) != contents:
            await pockets_service.set_svelte_source_file(
                pocket_id, user_id, component_path=path, new_source=contents
            )

    return results


async def get_native_artifact(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    builder_origin: str | None = None,
    _generator: GeneratorClient | None = None,
    _read_built: Callable[[str], tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Serve a svelte Paw Site's ARMED build as ``{pocket_id, body_html, css}`` so the
    native editor can shadow-render it (NE-5b) instead of framing an iframe.

    The native-editing render path. It:
      1. reads the pocket (the pockets service's PUBLIC ``get`` raises NotFound /
         Forbidden itself — a missing / cross-tenant pocket surfaces as 404 / 403)
         and asserts it is a svelte site (else ``ValidationError`` → 422), mirroring
         ``apply_leaf_edits`` / ``edit_svelte_component``;
      2. ENSURES the ARMED build: it builds the pocket with ``builder_origin`` set so
         the paw-sites generator stamps ``data-uid`` on the editable leaves AND embeds
         the ``<script id="paw-edit-manifest">`` (SE-1 / EP-6 gate on ``builderOrigin``).
         ``builder_origin`` defaults to the configured dashboard origin
         (``PAW_SITES_BUILDER_ORIGIN``) exactly like ``make_site_editable``, so the
         call works with no explicit origin. It builds via a DIRECT
         ``GeneratorClient.build`` — the SAME arm/preview gate ``make_site_editable``
         uses (``smoke=False`` skips the SSR FAIL-check but the static build still
         runs; ``static_build`` stays the default ``True`` so ``.svelte-kit/cloudflare/``
         is emitted) — instead of the full ``publish_pocket`` preview path, because
         this is a READ: it needs neither a deploy, a Site doc, a promote, nor billing,
         and ``build()`` RETURNS the ``project_dir`` so the built output is located
         without reconstructing a path. PERF-3 builds into the stable
         ``build_home()/<pocket_id>/`` so node_modules / bun install stay cached;
      3. reads the built ``<body>`` inner HTML + concatenated CSS from that
         ``project_dir``'s ``.svelte-kit/cloudflare/index.html`` (the SAME tree
         ``_default_bundle_reader`` / ``local_server`` read) and returns them.

    ``_generator`` (defaults to a real ``GeneratorClient``) and ``_read_built``
    (defaults to ``_read_native_artifact`` — the disk read + HTML/CSS extraction) are
    injectable seams so the path is unit-testable without Bun / a real build."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    # The pockets service's PUBLIC get raises NotFound / Forbidden itself (entity
    # isolation) — a missing / cross-tenant pocket surfaces as 404 / 403.
    pocket = await pockets_service.get(pocket_id, user_id)
    # KEPT svelte-specific (not is_source_engine): this shadow-renders the SvelteKit
    # ARMED build by reading ``.svelte-kit/cloudflare/index.html``. An html site's
    # served artifact IS its source, so it never uses this SvelteKit render path.
    if (pocket.get("engine") or "ripple") != "svelte" or not isinstance(pocket.get("source"), dict):
        raise ValidationError(
            "pocket.not_svelte_site",
            "This pocket is not a svelte Paw Site — it has no component build to render.",
        )
    source = pocket["source"]
    # theme rides the build on both engine tracks (mirrors publish_pocket); a svelte
    # pocket has no rippleSpec, so it resolves to {}.
    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    site_name = (pocket.get("name") or "").strip() or "Untitled site"

    # Arm the build: a NON-EMPTY builder_origin is what makes the generator stamp
    # data-uid + embed the manifest. Default to the configured dashboard origin when
    # the caller passes none, exactly like make_site_editable.
    origin = (builder_origin or "").strip() or _builder_origin()
    generator = _generator or GeneratorClient()
    # DEP-3: route the arm build through _build_or_cloud_error (the SAME helper the
    # publish paths at ~1152 / ~1293 use) so a missing-toolchain / non-zero build /
    # SmokeGateFailed becomes a clean CloudError (sites.generator_failed → 5xx)
    # instead of escaping as an opaque unhandled 500. Unlike edit_svelte_component's
    # preview build (map_smoke_gate=False, which preserves its
    # rollback-on-SmokeGateFailed contract), this is a READ-ONLY render with no source
    # mutation to roll back, so SmokeGateFailed is mapped too (default
    # map_smoke_gate=True) — the caller just gets a structured error.
    build = await _build_or_cloud_error(
        generator,
        ripple_spec=ripple_spec,
        theme=theme,
        # Transient, per-pocket-stable id — cosmetic here (it only rides the built
        # page's capture config, which the native editor ignores). The build DIR is
        # keyed on pocket_id (PERF-3), not this, so it does not affect where the
        # output lands.
        site_id=_preview_id(pocket_id),
        title=site_name,
        capture_api_base=_capture_base(),
        capture_signed_key=f"site_key_{secrets.token_urlsafe(24)}",
        engine="svelte",
        source=source,
        builder_origin=origin,
        pocket_id=pocket_id,
        # Arm/preview gate: skip the SSR FAIL-check but STILL emit the static
        # .svelte-kit/cloudflare/ output (static_build defaults True) so there is an
        # index.html to read.
        smoke=False,
    )

    read = _read_built or _read_native_artifact
    body_html, css = read(build.project_dir)
    return {"pocket_id": pocket_id, "body_html": body_html, "css": css}


async def edit_svelte_component(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    component_path: str,
    new_source: str | None = None,
    edits: list[dict[str, str]] | None = None,
    name: str = "",
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    _local_deploy: Callable[[str, str], str] | None = None,
) -> _SiteDoc:
    """Rewrite ONE component of a svelte Paw Site pocket and safely republish.

    The chat-agent entry point for a targeted component edit. The edit can be
    expressed two ways (exactly one is required):

      * ``edits`` (PREFERRED for small changes, P3) — a list of search/replace
        blocks ``[{old_string, new_string}, ...]`` the agent emits INSTEAD of the
        whole file. This reads the pocket's CURRENT ``component_path`` source,
        applies the blocks via ``apply_edits`` (each ``old_string`` must match
        exactly once), and uses the COMPUTED new source. The agent sends only the
        diff — the dominant token / latency saving over a full rewrite.
      * ``new_source`` (full rewrite, the SE-2 fallback) — the whole new file
        contents; used as-is. Reserve this for large rewrites.

    Either way the resolved new source replaces the file at ``component_path`` in
    the pocket's svelte ``source`` map and the site is republished. The Pocket
    write is owned by the pockets service (``set_svelte_source_file`` — entity
    isolation); this function only orchestrates resolve → persist → republish.

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

    # P3 — resolve the edit shape to a single ``new_source`` string. Exactly one of
    # ``edits`` (targeted diff) / ``new_source`` (full rewrite) must be supplied.
    if (edits is None) == (new_source is None):
        raise ValidationError(
            "site_edit.invalid_args",
            "edit_svelte_component requires exactly one of `edits` (a targeted "
            "search/replace diff) or `new_source` (a full file rewrite).",
        )
    if edits is not None:
        # Targeted/diff edit: read the pocket's CURRENT component source and apply
        # the blocks to compute the new source. The read goes through the pockets
        # service's PUBLIC ``get`` (wire dict — entity isolation; it raises
        # NotFound/Forbidden itself) so the apply runs against the source of truth.
        # A missing component path is a NotFound (same contract as the full-rewrite
        # path, where set_svelte_source_file raises it) — not a silent create.
        pocket = await pockets_service.get(pocket_id, user_id)
        # KEPT svelte-specific (not is_source_engine): edits a single named SvelteKit
        # component by ``component_path`` and persists via ``set_svelte_source_file``.
        # html has no per-component model — it edits by uid splice (HE-9), not here.
        if (pocket.get("engine") or "ripple") != "svelte" or not isinstance(
            pocket.get("source"), dict
        ):
            raise ValidationError(
                "pocket.not_svelte_site",
                "This pocket is not a svelte Paw Site — it has no component source map to edit.",
            )
        source_map = pocket["source"]
        if component_path not in source_map:
            raise NotFound("site_component", component_path)
        # apply_edits raises ValidationError (clear, retry-able) on any match-count
        # violation, BEFORE anything is persisted or rebuilt.
        new_source = apply_edits(source_map[component_path], edits)

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

    # 2. Build a PREVIEW of the edit (Branch primitive). The persist above already
    #    wrote a fresh DRAFT ArtifactVersion (set_svelte_source_file hooks it); the
    #    preview build smoke-gates + locally serves the working copy but does NOT
    #    promote that draft to published and does NOT overwrite the canonical live
    #    deploy. So an edit stays a reviewable draft (the prior live URL is
    #    untouched, get_draft is non-None, request_publish_pocket can submit it) —
    #    only an approved review (the real publish) takes the edit live.
    # 3. On a smoke-gate failure, restore the prior source so the pocket never
    #    carries a component the renderer rejects — then re-raise so the caller
    #    surfaces the reason. The prior deploy is untouched because the gate fires
    #    before publish deploys.
    try:
        return await publish_pocket(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            name=name,
            builder_origin=builder_origin or None,
            preview=True,
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
    """Arm a pocket's site for editing — build an editable PREVIEW (SE-2b + the
    Branch primitive).

    Backs ``POST /sites/by-pocket/{pocket_id}/editable``: it builds the pocket with
    ``builder_origin`` set so the generated page carries the gated edit-bridge, and
    returns the PREVIEW url the iframe frames. ``builder_origin`` defaults to the
    configured dashboard origin (``PAW_SITES_BUILDER_ORIGIN``) when the caller does
    not pass one, so the endpoint works with no body.

    Branch primitive: arming for editing is a PREVIEW, NOT a live publish. It
    delegates to ``publish_pocket`` with ``preview=True``, so it builds +
    smoke-gates + locally serves the working copy but does NOT promote the draft to
    published and does NOT overwrite the canonical live deploy/url. The pocket's
    draft survives (so a subsequent edit + ``request_publish_pocket`` works); only
    an approved review takes the edit live. It first ensures a draft snapshot
    exists (a pocket armed for editing that has never been edited would otherwise
    have no draft for the builder to frame / submit) via the same best-effort
    versions hook publish uses.

    It inherits ``publish``'s NotFound / Forbidden propagation and the smoke gate —
    a build that fails the gate raises ``SmokeGateFailed`` and the prior live
    deploy is untouched.
    """
    origin = (builder_origin or "").strip() or _builder_origin()

    # Ensure a draft snapshot exists so the armed-for-editing pocket has a working
    # copy to frame and submit, even before the first component edit. Snapshots the
    # pocket's current engine content (rippleSpec / svelte source map). Best-effort
    # — versioning is an additive layer, never a gate on arming.
    await _ensure_pocket_draft(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)

    return await publish_pocket(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        builder_origin=origin,
        preview=True,
        _generator=_generator,
        _cloudflare=_cloudflare,
        _bundle_reader=_bundle_reader,
        _local_deploy=_local_deploy,
    )


async def _ensure_pocket_draft(*, workspace_id: str, user_id: str, pocket_id: str) -> None:
    """Ensure the pocket has a current DRAFT version (Branch primitive).

    Arming a site for editing must leave a draft for the builder to frame and for
    ``request_publish_pocket`` to submit. A pocket that was published but never
    edited has only a ``published`` version (no draft), so this writes a draft
    snapshot of the pocket's current engine content when none exists. It is a
    no-op when a draft is already present (the common path — an edit already wrote
    one). Reads the pocket through the pockets service (wire dict — entity
    isolation) to resolve the engine + content.

    Best-effort: versioning is an additive history/Branch layer, never a gate on
    arming, so a missing module / read failure is logged and swallowed.
    """
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service
        from pocketpaw_ee.versions import service as versions_service

        existing = await versions_service.get_draft(
            scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id
        )
        if existing is not None:
            return
        pocket = await pockets_service.get(pocket_id, user_id)
        engine = pocket.get("engine") or "ripple"
        if is_source_engine(engine):
            content = pocket.get("source") if isinstance(pocket.get("source"), dict) else {}
        else:
            content = pocket.get("rippleSpec") if isinstance(pocket.get("rippleSpec"), dict) else {}
        await versions_service.write_draft(
            scope_type=_VERSION_SCOPE_TYPE,
            scope_id=pocket_id,
            workspace_id=workspace_id,
            content=content or {},
            author=user_id,
        )
    except Exception:  # noqa: BLE001 — versioning must not break arming for edit
        logger.warning(
            "versions: failed to ensure a draft for pocket %s on arm-for-edit — "
            "preview proceeds, draft snapshot skipped",
            pocket_id,
            exc_info=True,
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
    if is_source_engine(engine):
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


async def dev_preview_pocket(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    builder_origin: str | None = None,
) -> DevPreviewResponse:
    """Ensure a live Vite dev-server is running for the pocket and return its URL
    (Phase 2 / P2a — the EDITING preview).

    Delegates to the DevServerManager singleton: a running server for the pocket is
    touched + reused (its URL returned); otherwise the manager materializes the
    pocket's current source into the persistent per-pocket build dir (PERF-3 —
    cached node_modules) and starts ``vite dev`` on an ephemeral port, so subsequent
    edits hot-reload over Vite HMR in ~ms instead of rebuilding the whole site. The
    workerd smoke render is NOT run for the dev server (it is a publish-only gate,
    PERF-4); publish() is unchanged and still does the full prod build + smoke.

    ``builder_origin`` (S1) makes the dev-materialized SOURCE carry SE-1's gated
    section anchors + postMessage edit-bridge so the hover-edit overlay works against
    the dev server. It is resolved the SAME way ``make_site_editable`` resolves it —
    the passed origin (the request ``Origin`` header at the router) when present, else
    the configured ``PAW_SITES_BUILDER_ORIGIN`` (``_builder_origin()``) — so the dev
    source is anchored + bridged exactly like the static editable build. It is threaded
    to the manager → ``_default_materialize`` → ``GeneratorClient.build`` and rides
    ``siteConfig.builderOrigin``; the generator gates the injection on it, so an
    empty origin still produces a non-bridged source (the gate holds). The dev path
    keeps ``static_build=False`` — only the generate/scaffold step needs the origin.

    ``user_id`` is threaded through so the manager reads the pocket via the pockets
    service under the caller's scope (it raises NotFound / Forbidden itself, mapped
    by the router to 404 / 403). ``workspace_id`` keeps the surface uniform and
    tenant-aware with the other by-pocket reads.
    """
    from pocketpaw_ee.sites.dev_server import get_manager

    # Mirror make_site_editable's origin resolution: the passed origin (request
    # Origin header) when present, else the PAW_SITES_BUILDER_ORIGIN env fallback.
    origin = (builder_origin or "").strip() or _builder_origin()

    url = await get_manager().ensure_dev_server(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        builder_origin=origin,
    )
    return DevPreviewResponse(pocket_id=pocket_id, url=url)


async def audit_pocket(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
) -> AuditResponse:
    """Run the deterministic site audit over a pocket's content (BP-7, producer 2).

    Reads the pocket's content the SAME way ``preview_pocket`` does — the BP-1
    DRAFT-version snapshot (what publish WOULD build), falling back to the pocket's
    current rippleSpec / source map when there is no draft row — then runs the pure
    deterministic audit engine (``sites.audit.audit_pocket_site``) over it. The
    audit itself is side-effect free; this function only resolves the content.

    Each finding carries a ``fix_prompt`` the UI feeds to the EXISTING edit path
    (``edit_svelte_component`` / refine), which lands the fix as a reviewable draft
    in the Tray — BP-7 adds NO apply endpoint. A clean site returns an empty
    ``findings`` list.

    The pockets service's PUBLIC ``get`` raises NotFound / Forbidden itself when
    the pocket is missing or access-denied (mapped to 404 / 403 by the router), so
    no extra existence check is needed. ``workspace_id`` keeps the surface uniform
    and tenant-aware (the pockets read scopes on ``user_id``)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites.audit import audit_pocket_site

    pocket = await pockets_service.get(pocket_id, user_id)
    engine = pocket.get("engine") or "ripple"

    # The pocket's CURRENT content for the engine — the fallback when the Branch
    # primitive has no draft row for this pocket yet (mirrors preview_pocket).
    if is_source_engine(engine):
        source = pocket.get("source")
        current: dict[str, Any] | None = source if isinstance(source, dict) else None
    else:
        ripple_spec = pocket.get("rippleSpec")
        current = ripple_spec if isinstance(ripple_spec, dict) else None

    # Prefer the DRAFT version's snapshot (the working copy publish would build).
    # Versioning is an additive layer — a missing module / read failure must not
    # break the audit, so degrade to the current content on any error.
    draft_content: dict[str, Any] | None = None
    try:
        from pocketpaw_ee.versions import service as versions_service

        draft = await versions_service.get_draft(scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id)
        if draft is not None:
            draft_content = draft.content
    except Exception:  # noqa: BLE001 — versions read is best-effort
        logger.warning(
            "versions: failed to read draft for pocket %s audit — falling back to current content",
            pocket_id,
            exc_info=True,
        )

    content = draft_content if draft_content is not None else current
    findings = audit_pocket_site(engine=engine, content=content)
    return AuditResponse(
        pocket_id=pocket_id,
        engine=engine,
        findings=[AuditFinding(**f) for f in findings],
    )


# DS-3 — the reason string the local/dev-mode degradation surfaces. The data
# behind a dynamic site lives in a per-tenant Cloudflare D1 reachable only on a
# live CF deploy; local mode has no D1, so the read returns this instead of an
# error (the schema is still listed from the spec).
_DATA_UNAVAILABLE_LOCAL = "live_on_cloudflare_only"


def _dynamic_content_envelope(pocket: dict[str, Any]) -> dict[str, Any]:
    """The ENGINE-APPROPRIATE content envelope a dynamic site's bindings live on
    (DSV-2b).

    A dynamic pocket carries its live-data bindings — ``objects`` (the D1 tables),
    ``sources`` (reads), ``actions`` (writes), optional ``auth`` — as SIBLING KEYS
    on its content. WHICH content holds them depends on the generation engine, and
    must match the publish/promote switch (``version_content = source if engine ==
    "svelte" else ripple_spec``):

      * ``engine == "svelte"`` → the bindings are siblings on the svelte ``source``
        envelope (the same dict that also carries the ``{path: contents}``
        hand-written SvelteKit files). This is the CONTRACT the create-svelte brain
        + the generator must store to: ``objects``/``sources``/``actions``/``auth``
        sit alongside the file entries on ``source``.
      * any other engine (``"ripple"``, the default) → the bindings are siblings on
        ``rippleSpec`` (the ripple-track precedent the create-dynamic-site tool
        already stamps).

    Returns the selected dict (``{}`` when absent / malformed), so the
    engine-agnostic ``_is_dynamic`` / ``_dynamic_objects`` helpers can read the
    bindings off it without caring which engine produced it."""
    engine = pocket.get("engine") or "ripple"
    key = content_key(engine)
    content = pocket.get(key)
    return content if isinstance(content, dict) else {}


async def _dynamic_pocket_objects(
    *, workspace_id: str, user_id: str, pocket_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a pocket and return ``(content_envelope, objects)`` for a DYNAMIC site,
    or raise (DS-3 shared resolver; DSV-2b made it engine-aware).

    Loads the pocket via the pockets service's PUBLIC ``get`` (wire dict — entity
    isolation; it raises NotFound / Forbidden itself for a missing / access-denied
    pocket, mapped to 404 / 403). The dynamic bindings (``objects`` and friends)
    are read off the ENGINE-APPROPRIATE content envelope (DSV-2b): a svelte site's
    bindings live on its ``source`` map, a ripple site's on its ``rippleSpec`` —
    see ``_dynamic_content_envelope``. A NON-dynamic pocket (a static landing /
    brochure, or a custom pocket with no data bindings) raises
    ValidationError("sites.not_dynamic") → the router maps it to 400: there is no
    data store to read. The returned ``objects`` are the envelope's declared tables
    (the authoritative table set a read may touch). ``workspace_id`` keeps the
    surface tenant-uniform with the other by-pocket reads (the pockets read scopes
    on ``user_id``; the D1 id derivation is workspace-scoped)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(pocket_id, user_id)
    pattern = pocket.get("pattern")
    content = _dynamic_content_envelope(pocket)
    if not _is_dynamic(pattern, content):
        raise ValidationError(
            "sites.not_dynamic",
            "This site is not a dynamic site — it has no live data store to read.",
        )
    return content, _dynamic_objects(content)


def _table_columns(obj: dict[str, Any]) -> list[str]:
    """The declared column names of a spec ``objects`` table (DS-3). ``fields`` is
    a {column: type} map; an absent / malformed ``fields`` yields no columns."""
    fields = obj.get("fields")
    return list(fields.keys()) if isinstance(fields, dict) else []


async def list_site_data_tables(
    *, workspace_id: str, user_id: str, pocket_id: str
) -> SiteDataTablesResponse:
    """List a dynamic site's tables for the operator data-view (DS-3).

    Backs ``GET /sites/by-pocket/{pocket_id}/data``. The table LIST is always read
    from the pocket spec's ``objects`` (the declared D1 tables), so it is populated
    even when the live D1 data is not reachable. ``available`` reflects whether the
    ROWS behind those tables can actually be read:
      * in local/dev mode (``_local_mode()`` — PAW_SITES_LOCAL=1 or no
        PAW_CF_ACCOUNT_ID) there is no live D1, so ``available`` is False with
        ``reason="live_on_cloudflare_only"``; the UI still shows the schema and an
        explanatory empty state instead of erroring;
      * with a live Cloudflare deploy, ``available`` is True (the per-table read
        then returns rows).

    A NON-dynamic pocket raises ValidationError("sites.not_dynamic") → 400 (no data
    store). A missing / access-denied pocket surfaces as 404 / 403 via the pockets
    service. Tenant-scoped: the pockets read scopes on ``user_id``; the D1 id (when
    a live read happens) is derived per (workspace, pocket)."""
    _spec, objects = await _dynamic_pocket_objects(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    tables = [
        SiteDataTableInfo(
            name=obj["name"],
            fields=obj.get("fields") if isinstance(obj.get("fields"), dict) else {},
            primary_key=obj.get("primaryKey") if isinstance(obj.get("primaryKey"), str) else "",
        )
        for obj in objects
    ]
    available = not _local_mode()
    return SiteDataTablesResponse(
        pocket_id=pocket_id,
        available=available,
        reason="" if available else _DATA_UNAVAILABLE_LOCAL,
        tables=tables,
    )


async def read_site_data_table(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    table: str,
    _cloudflare: Any | None = None,
) -> SiteDataRowsResponse:
    """Read the rows of ONE table of a dynamic site's D1 (DS-3).

    Backs ``GET /sites/by-pocket/{pocket_id}/data/{table}``. The flow:
      1. resolve the pocket's declared ``objects`` (a non-dynamic pocket → 400);
      2. VALIDATE ``table`` against those declared names — an unknown table raises
         NotFound("site_table") → 404. This is the SQL-safety gate: the table
         identifier reaching the query is ALWAYS one of the spec's known object
         names, never attacker-supplied free text, so it is safe to embed as the
         FROM identifier (D1 / SQLite cannot bind an identifier as a placeholder).
         Every VALUE still binds through ``params``; the only interpolated token is
         this whitelisted identifier;
      3. in local/dev mode (``_local_mode()``) there is NO live D1 — return a clean
         ``available=False`` / ``reason="live_on_cloudflare_only"`` shape with the
         table's declared ``columns`` and empty ``rows`` (the UI degrades cleanly);
      4. otherwise derive the D1 database id (the Site doc's ``d1_database_id`` if
         present — DS-2 forward-compat — else the deterministic
         ``_derive_d1_database_id``) and run a bounded ``SELECT * FROM <table>
         LIMIT ?`` via the Cloudflare D1 query API, returning the rows.

    The row count is capped (``_data_row_limit()``) so the data-view stays a
    recent-records list, not an unbounded export. Tenant-scoped: the pockets read
    scopes on ``user_id``; the D1 id is per (workspace, pocket), and the Site doc
    read filters on ``workspace`` — a foreign workspace cannot read another
    tenant's data. ``_cloudflare`` is injectable so the path is unit-testable
    without a live D1."""
    _spec, objects = await _dynamic_pocket_objects(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )

    # SQL-safety gate: the requested table MUST be one of the spec's declared
    # object names. An unknown table is a 404 — never reaches a query, never
    # interpolated. ``next`` finds the matching object (for its declared columns).
    target = next((obj for obj in objects if obj.get("name") == table), None)
    if target is None:
        raise NotFound("site_table", table)
    columns = _table_columns(target)

    # Local/dev mode: no live D1 to read. Degrade cleanly — list the declared
    # columns from the spec, return no rows, and say why.
    if _local_mode() and _cloudflare is None:
        return SiteDataRowsResponse(
            pocket_id=pocket_id,
            table=table,
            available=False,
            reason=_DATA_UNAVAILABLE_LOCAL,
            columns=columns,
            rows=[],
        )

    # Resolve the D1 database id: prefer a stored id (DS-2's Site.d1_database_id,
    # via getattr so this branch builds without DS-2), else derive it
    # deterministically so the READ targets the SAME db a deploy bound.
    doc = await _canonical_site_doc(workspace_id, pocket_id)
    stored_db_id = getattr(doc, "d1_database_id", "") if doc is not None else ""
    db_id = stored_db_id or _derive_d1_database_id(workspace_id, pocket_id)

    cf = _cloudflare or _cf_client()
    # ``table`` is whitelisted above (it equals a declared object name), so it is
    # safe to embed as the FROM identifier — SQLite/D1 cannot bind an identifier as
    # a placeholder. The LIMIT VALUE binds through ``params``.
    limit = _data_row_limit()
    rows = await cf.query_d1(
        database_id=db_id,
        sql=f"SELECT * FROM {table} LIMIT ?",  # noqa: S608 — table is whitelisted, value is bound
        params=[limit],
    )
    return SiteDataRowsResponse(
        pocket_id=pocket_id,
        table=table,
        available=True,
        reason="",
        columns=columns,
        rows=rows,
    )


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

    PERF-1: the read is now the CANONICAL Site doc for (workspace, pocket_id), not
    an arbitrary ``find_one`` across dupes. With stable identity a pocket has ONE
    doc; but PERF-1 does NOT migrate the dupes the old minting left behind (that's
    PERF-2), so to fix the stale-live-link bug today we pick the canonical doc
    deterministically — the stable-id doc when present, else the newest doc that
    actually carries a url — and surface its (non-null, latest) ``url`` so the live
    link no longer points at a stale ``url=None`` row.
    """
    doc = await _canonical_site_doc(workspace_id, pocket_id)

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

    # P2b: surface the canonical doc's last-deploy time so the builder/gallery can
    # render "Last deployed <time>" without a second fetch. None when the pocket has
    # no deployed site or the doc predates the field (a pre-P2b row).
    deployed_at = getattr(doc, "deployed_at", None) if doc is not None else None

    # DS-1a/SR-9: resolve the source pocket's authoring pattern + engine so a
    # by-pocket status read can badge a dynamic site AND its engine too. Each is
    # ONE read, tenant-scoped; "" when the pocket has no value or could not be
    # resolved (empty-safe).
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    patterns = await pockets_service.patterns_for_pockets(workspace_id, [pocket_id])
    pattern = patterns.get(pocket_id) or ""
    engines = await pockets_service.engines_for_pockets(workspace_id, [pocket_id])
    engine = engines.get(pocket_id) or ""

    return SiteStatusResponse(
        pocket_id=pocket_id,
        status=status,
        is_live=is_live,
        has_unpublished_changes=has_unpublished_changes,
        site_id=str(doc.id) if doc is not None else None,
        # PERF-1: surface the canonical doc's live url so the builder/gallery link to
        # the address the latest build actually serves at, not a stale ``url=None``
        # dupe. None when the pocket has no deployed site.
        url=(doc.url or None) if doc is not None else None,
        deployed_at=deployed_at.isoformat() if deployed_at is not None else None,
        pattern=pattern,
        engine=engine,
    )


async def request_publish_pocket(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
) -> Any:
    """Submit a pocket's current draft for human review (BP-4 Part C).

    The clean entry to the Branch-primitive MERGE GATE: instead of a client
    hand-building the Instinct ``_artifact_change`` proposal (and getting the
    blob shape / tenancy wrong), it POSTs here and the SERVER constructs the
    review Action via the Instinct store. The created Action is the gate item an
    operator approves in The Tray; approving it dispatches BP-3's merge executor
    (publish the candidate version + deploy), so this is the request-publish →
    review → approve → published round-trip's first step.

    The versionable artifact behind a Paw Site is the source pocket
    (scope_type="pocket", scope_id=pocket_id — the same scope BP-2 keys site
    versions on). The proposal's ``_artifact_change`` blob carries:
      * ``from_version_id`` — the currently published version id (or None when
        nothing is live yet — a first publish);
      * ``to_version_id``   — the current DRAFT version id (the working copy the
        operator is being asked to take live).

    Tenancy: the blob's ``workspace`` is stamped with ``workspace_id`` (NEVER
    empty — BP-3's ``_assert_artifact_change_workspace`` hard-403s an empty
    workspace claim, so a real workspace MUST be set here for the gate to ever
    approve). The caller passes its ``ctx.workspace_id``.

    P0b — SELF-HEAL legacy sites: a site published before BP-1 has ZERO
    ``artifact_versions`` rows, so ``get_draft`` returns None even though the
    pocket has live content. Rather than 400, this BACKFILLS a draft snapshot of
    the pocket's current content (via ``_ensure_pocket_draft``) and proceeds. The
    400 is kept ONLY for a genuinely empty / nonexistent pocket (nothing to
    snapshot) or a foreign-workspace draft (tenant isolation).

    Raises ``ValueError`` when there is NO current draft to publish AND none can
    be backfilled (the router maps it to a 4xx — there is nothing to submit for
    review). A missing / access-denied pocket surfaces via the pockets service
    (NotFound, swallowed by the best-effort backfill) → still no draft → 400.
    """
    from pocketpaw.instinct.models import (
        ActionCategory,
        ActionPriority,
        ActionTrigger,
    )
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.versions import service as versions_service

    draft = await versions_service.get_draft(scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id)
    if draft is None or draft.workspace_id != workspace_id:
        # P0b — a site PUBLISHED before BP-1 has ZERO artifact_versions rows, so it
        # has no draft lineage at all. That is NOT "nothing to review": the pocket
        # still has live content the operator wants to submit. BACKFILL a draft
        # snapshot of the pocket's CURRENT content (reusing ``_ensure_pocket_draft``,
        # which reads the engine + content via the pockets service and writes a draft
        # only when none exists), then re-read. This self-heals the legacy site so
        # Submit-for-review works on the first click instead of 400'ing — and the
        # edits no longer leak to live, because they land on a draft the merge gate
        # must approve. A genuinely empty / nonexistent pocket still has nothing to
        # snapshot (``_ensure_pocket_draft`` swallows the pockets-service NotFound),
        # so ``get_draft`` stays None below and we keep the 400.
        await _ensure_pocket_draft(workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id)
        draft = await versions_service.get_draft(scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id)

    if draft is None or draft.workspace_id != workspace_id:
        # Still nothing after the backfill attempt — a genuinely empty / nonexistent
        # pocket, or a foreign-workspace draft (tenant isolation, the same guard
        # ``pocket_status`` applies). Nothing to review.
        raise ValueError(f"no draft version to publish for pocket {pocket_id} — nothing to review")

    published = await versions_service.get_published(
        scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id
    )
    from_version_id = (
        str(published.id)
        if published is not None and published.workspace_id == workspace_id
        else None
    )
    to_version_id = str(draft.id)

    # The merge-gate blob. Shape MUST match BP-3's ``_artifact_change_blob``
    # (instinct/router.py) + the executor's reader exactly: the executor pulls
    # scope_type/scope_id/workspace/to_version_id off it on approve. ``branch``
    # is "main" (the published lineage — this is a publish, not a candidate
    # branch). ``workspace`` is the canonical key; the executor also accepts
    # ``workspace_id`` as an alias.
    blob = {
        "schema": 1,
        "scope_type": _VERSION_SCOPE_TYPE,
        "scope_id": pocket_id,
        "branch": "main",
        "from_version_id": from_version_id,
        "to_version_id": to_version_id,
        "workspace": workspace_id,
        "user_id": user_id,
        "correlation_id": None,
        "proposed_event_id": None,
    }

    # ISO: scope the store to the caller's workspace (this can run on an HTTP
    # path with no ``current_workspace`` ContextVar) so the publish proposal
    # lands in the tenant's file.
    store = get_instinct_store(workspace_id=workspace_id or None)
    action = await store.propose(
        pocket_id=pocket_id,
        title="Publish site changes",
        description=(
            "Take the current draft of this site live. Approving merges the "
            "reviewed version and deploys it."
        ),
        recommendation="Review the draft, then approve to publish.",
        trigger=ActionTrigger(
            type="user",
            source="request-publish",
            reason="Operator requested the pocket's draft be published for review",
        ),
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.MEDIUM,
        parameters={"_artifact_change": blob},
        workspace_id=workspace_id,
        scope_type=_VERSION_SCOPE_TYPE,
    )
    return action


async def list_for_workspace(workspace_id: str) -> list[SiteResponse]:
    """The gallery / listSites read: the workspace's Site cards, newest first.

    PERF-2: filters out ARCHIVED docs so each pocket shows exactly one card. The
    pre-PERF-1 minting left a pile of duplicate Site docs per pocket (one pocket
    had 14), all of which this read listed → the gallery duplicated. The dedupe
    migration (``sites.dedupe``) keeps ONE canonical doc per pocket active and
    tombstones the rest with ``archived=True``; excluding them here collapses the
    gallery to one card per pocket. ``archived: {"$ne": True}`` (not ``False``)
    so docs predating the field — which have no ``archived`` key in Mongo — still
    count as active.

    DS-1a: each card also carries its source pocket's authoring ``pattern``
    ("dynamic" | "landing" | ...) so the frontend can badge dynamic sites. The
    pattern lives on the source Pocket, not the Site, so it is resolved in ONE
    batch read (``pockets_service.patterns_for_pockets`` — no N+1) keyed on the
    listed pockets, then attached per card. A pocket with no pattern (or one that
    could not be resolved) reads "" so the gallery is empty-safe.
    """
    cursor = _SiteDoc.find({"workspace": workspace_id, "archived": {"$ne": True}}).sort(
        -_SiteDoc.createdAt
    )  # type: ignore[operator]
    docs = [doc async for doc in cursor]
    # ONE cross-entity read per field for every card's pattern + engine (no
    # per-site fetch). The Pocket reads stay in the pockets service (entity
    # isolation) — this service never imports the Pocket model. SR-9 adds the
    # engine resolution alongside DS-1a's pattern; each is a single projected
    # $in query keyed on the listed pockets.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket_ids = [doc.pocket_id for doc in docs]
    patterns = await pockets_service.patterns_for_pockets(workspace_id, pocket_ids)
    engines = await pockets_service.engines_for_pockets(workspace_id, pocket_ids)
    return [
        _to_response(
            doc,
            patterns.get(doc.pocket_id) or "",
            engines.get(doc.pocket_id) or "",
        )
        for doc in docs
    ]


async def site_pocket_ids(workspace_id: str) -> set[str]:
    """Return the set of ``pocket_id``s that have a published Site in this
    workspace.

    Lets the /pockets gallery hide pockets that have been published as a Site
    (they show under /sites instead) WITHOUT the pockets service importing the
    Site Beanie model — the Site read stays in this service, which is the sole
    owner of Site reads (entity isolation). Tenant-scoped on ``workspace``.

    PERF-2: excludes ARCHIVED dupes (``archived: {"$ne": True}``) so the set is
    keyed on pockets that still have an ACTIVE Site — a fully-archived pocket
    would be wrong to hide from the /pockets gallery. Pre-field docs (no
    ``archived`` key) still count as active.
    """
    cursor = _SiteDoc.find({"workspace": workspace_id, "archived": {"$ne": True}})
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


async def version_history(*, workspace_id: str, pocket_id: str) -> list[Any]:
    """The ordered version timeline for a pocket (BP-4 Part B).

    Backs ``GET /sites/by-pocket/{pocket_id}/versions``: the version log for the
    source pocket (scope_type="pocket"), oldest → newest, tenant-scoped on
    ``workspace_id``. Reads the ArtifactVersion rows directly via
    ``versions.list_versions`` — the rows ARE the ordered log (monotonic
    ``version_no``), so the timeline is exact and current with no projection
    replay. (The VersionProjection is the BP-4 deliverable for the EVENT history
    view — what happened, in order; this endpoint shows the VERSION timeline,
    which the rows serve directly and correctly.)

    Tenant isolation: the BP-1 pointer/log reads key only on
    (scope_type, scope_id) — artifact-generic, no workspace param — so we filter
    the returned rows on the caller's ``workspace_id`` here, exactly as
    ``pocket_status`` does, so a foreign workspace cannot read another tenant's
    history through a known pocket id. Returned oldest → newest (the natural
    reading order for a history view; list_versions returns newest-first).
    """
    from pocketpaw_ee.versions import service as versions_service

    rows = await versions_service.list_versions(
        scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id, branch="main"
    )
    scoped = [r for r in rows if r.workspace_id == workspace_id]
    # list_versions returns newest-first; a timeline reads oldest → newest.
    return list(reversed(scoped))


async def revert_pocket_version(
    *, workspace_id: str, user_id: str, pocket_id: str, version_no: int
) -> Any:
    """Revert a pocket's site to a prior version by ordinal (P2b-backend).

    Backs ``POST /sites/by-pocket/{pocket_id}/versions/{version_no}/revert``.
    Revert is FORWARD-MOVING: it writes a NEW draft (on the main branch) whose
    content is a snapshot of the target version's content, then the normal
    review/publish flow applies (the operator can request-publish that draft and
    take the reverted content live through the merge gate). It never mutates
    history — the version log stays append-only and the revert is its own
    auditable lineage step.

    The router carries the human-friendly ``version_no`` (the timeline ordinal the
    UI shows); the versions ``revert`` keys on the durable ``version_id``, so this
    resolves the ordinal → the row via the SAME tenant-scoped, main-branch log
    ``version_history`` reads. A version_no the pocket does not have (or one under
    another workspace — the rows are pre-filtered on ``workspace_id``) raises
    ``ValueError`` (the router maps it to a 404). Returns the new draft
    ``ArtifactVersion``.
    """
    from pocketpaw_ee.versions import service as versions_service

    rows = await versions_service.list_versions(
        scope_type=_VERSION_SCOPE_TYPE, scope_id=pocket_id, branch="main"
    )
    target = next(
        (r for r in rows if r.version_no == version_no and r.workspace_id == workspace_id),
        None,
    )
    if target is None:
        raise ValueError(f"no version v{version_no} for pocket {pocket_id} — cannot revert")

    return await versions_service.revert(
        scope_type=_VERSION_SCOPE_TYPE,
        scope_id=pocket_id,
        workspace_id=workspace_id,
        version_id=str(target.id),
        author=user_id,
    )


__all__ = [
    "apply_edits",
    "publish",
    "publish_pocket",
    "activate_site",
    "preview_pocket",
    "pocket_status",
    "list_site_data_tables",
    "read_site_data_table",
    "request_publish_pocket",
    "version_history",
    "revert_pocket_version",
    "edit_svelte_component",
    "make_site_editable",
    "require_sites_plan",
    "add_domain",
    "domain_status",
    "list_domains",
    "list_for_workspace",
    "site_pocket_ids",
    "reserve_local_sites",
]
