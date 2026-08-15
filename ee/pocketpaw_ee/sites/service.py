# ee/pocketpaw_ee/sites/service.py — Sites control-plane orchestration. Sole
# owner of Site writes.
#
# Updated 2026-08-12 (sites Settings consolidation): added ``get_site_client`` /
# ``update_site_client`` / ``record_site_invoice`` — the owner's record of who a
# site is FOR and what they have billed them. Two decisions worth knowing before
# editing them. (1) The PATCH is THREE-WAY: absent means "leave alone", explicit
# "" means "clear", read off ``model_fields_set``. A two-way patch would let an
# autosaving form blank the fields the user did not touch. (2) Both writers use a
# targeted ``set()``, not ``save()`` — this is a human-paced edit against a
# document the BUILD lane also writes, so a full-document save could push a stale
# snapshot back and roll ``build_status`` backwards while an owner types notes.
#
# Updated 2026-08-11 (RX-4 — the agent can tell whether a site is actually live): added
# ``build_wire_state`` (pure) and ``site_build_status`` (a read). ``_to_response``
# already gave the frontend ``build_status`` / ``build_reason`` / ``build_job_id``, and
# the frontend polls them next to ``url`` knowing a site can be live and simultaneously
# mid-rebuild. The chat agent had neither the fields nor that knowledge, and on react —
# the only engine where ``build_runs_async`` is True — that meant a first publish handed
# it ``url=""`` and a re-publish handed it the PREVIOUS deploy's url, both reported as
# success. ``build_wire_state`` derives ``build_in_progress`` and ``is_live`` from the
# row once, so the publish response and the status tool cannot disagree; the raw status
# still passes through verbatim. ``build_in_progress`` reads an unknown status as IN
# PROGRESS, deliberately the OPPOSITE of ``build_state.should_enqueue`` (a redundant
# build costs one sandbox; a spurious "your site is live" costs trust), and is derived
# from ``TERMINAL_STATUSES`` so a new state defaults to in-progress here for free.
# ``site_build_status`` exists because an async publish returns before the build starts:
# without a later read, "queued" is a dead end.
#
# Updated 2026-08-11 (RX-3 — the react track gets an EDIT lane): added
# ``edit_react_component``, the react peer of ``edit_svelte_component``. Until this
# existed there was no way to change a react site: this module's edit entry points
# are svelte-gated, so the chat agent's only response to "shorten the hero
# headline" was a second ``create_react_site``, which mints a SECOND site pocket.
# Two contracts differ from the svelte peer on purpose, and both are argued in the
# function's own docstring so a reader who only opens that one does not have to
# guess:
#   * DRAFT-ONLY — it does NOT republish and does NOT enqueue a build. It cannot:
#     ``build_runs_async("react")`` is True, so a react publish enqueues a Daytona
#     build and returns before any outcome exists. There is nothing synchronous to
#     roll back from, and a rollback fired on enqueue-success would revert a good
#     edit. Persisting the draft IS the job (the shape ``apply_leaf_edits`` already
#     documents). Publishing stays the user's call.
#   * A ``create`` flag for a NEW component file, because "add a testimonials
#     section" needs one plus an ``src/App.tsx`` edit. It INVERTS the existence
#     check rather than relaxing it.
# The path guard is shared with create through the new
# ``sites/react_paths.py`` — an edit that could write ``package.json`` would be a
# way around the generator's dependency allowlist, and with it the supply-chain
# release-age floor that manifest is what enforces.
#
# Updated 2026-08-10 (SL-3 — the build lane reaches the wire): ``_to_response`` and
# ``pocket_status`` now populate ``build_status`` / ``build_reason`` / ``build_job_id``
# from the Site row. They were declared on the DTOs by SG-9i and never passed here, so
# every response carried the defaults — ``build_status`` frozen at "none" regardless of
# the row — and the shipped build-status UI was polling a field that could not change.
# Read via ``getattr`` defaults like every other field, so a pre-SG-9i row reads as "no
# build" instead of raising, and a pocket with no Site doc reads "none" rather than null
# (a draft that was never published has no build, which is not the same as a failed one).
#
# ``build_status`` is passed through VERBATIM, never normalised against a known set: the
# wire's contract is that a client treats an unrecognised status as IN-PROGRESS, and
# folding an unknown value into "none" here would break that from the server side.
#
# THE PUBLISH PATH IS UNCHANGED BY SL-3. ``_deploy_site_doc`` still builds and deploys
# inline for a static site. Flipping it to enqueue-and-return is blocked on a
# prerequisite that does not exist yet: the ephemeral lane's artifact is a tar of the
# static output only, ``sites/build_job.py`` never persists it, and no deploy target
# accepts one — ``local_server.deploy_local`` and ``workers_deploy.deploy_workers`` both
# want a project DIR, and the wfp path wants a worker bundle read out of one. Worse, for
# ripple and dynamic svelte the artifact cannot serve at all: its pages come from a
# ``_worker.js`` whose imports sit OUTSIDE the tarred directory, which is precisely why
# ``truth_lane`` refuses to even preview one (``REASON_WORKER_RENDERED``). Flipping today
# would take those sites from "publishes and works" to "queues a build nothing can
# deploy".
#
# Updated 2026-08-10 (SL-2 slice 2 — the ephemeral-build lane gets a job): added the
# four seams the site-build arq job (``sites/build_job.py``) writes its lifecycle
# through — ``load_build_site``, ``claim_build_queued``, ``mark_build_running``,
# ``record_build_outcome`` — at the bottom of the DP0-3 seam block, which they are
# modelled on. This module stays the sole owner of Site writes; the build lane never
# touches the Beanie doc.
#
# THE PUBLISH PATH IS UNCHANGED BY THAT SLICE, deliberately. ``publish`` /
# ``_deploy_site_doc`` still build synchronously through the local generator and do not
# enqueue anything. Flipping publish to enqueue-and-return is a later slice, gated on a
# frontend that can render a queued build — ship the flip first and every publisher sees
# a finished-looking page for a site that has not built yet.
#
# Unlike every other write in this module, the four build seams use a TARGETED ``set``
# rather than ``save()``: a build runs for minutes next to a publish that may be writing
# ``url`` / ``deployed`` on the same row, and a full save from a stale doc would roll
# those back. See the seam block for the full note.
#
# Updated 2026-08-07 (SC-1 — a site's card shows its own screenshot): the tail of
# a SUCCESSFUL deploy now also schedules a screenshot of the page it just put
# live (``_schedule_site_screenshot``, next to the knowledge sync it is modelled
# on), and ``_to_response`` / ``pocket_status`` surface the resulting
# ``preview_image_url`` on both DTOs so the gallery card can render the page
# instead of a title and three pills. Both live-deploy paths schedule it —
# ``_deploy_site_doc`` (static / inline publish) and ``finalize_provisioned_site``
# (the durable dynamic provision job) — because those are the two places a site
# actually becomes reachable. The scheduling call is wrapped exactly like the KB
# sync: a screenshot is a picture of a site that is ALREADY deployed and serving,
# so nothing about it may fail, delay, or block the publish. On any failure the
# field stays empty and the card falls back to its text layout.
#
# Updated 2026-08-07 (SC-2 — drafts get art too): ``create_draft_site`` now schedules
# a capture of its own (``_schedule_draft_screenshot``). A draft has no url, so that
# capture shoots the pocket's MARKUP rather than a page (``sites.draft_markup`` +
# the Browser Rendering ``html`` body). It fires ONLY on a fresh mint — the mint is
# idempotent, so a repeat create re-shoots nothing and an already-live doc never
# gets a draft picture — and it is wrapped for a reason the live path does not have:
# ``create_draft_site`` is called from the zip/from-url import tail WITHOUT a
# swallow of its own, so an escaping error there would fail an import whose files
# are already safely persisted.
# The PREVIEW branch of ``publish`` schedules one too
# (``_schedule_draft_screenshot_for_pocket``), for a cost reason: a preview has just
# built the pocket, so the markup is on disk and the capture is a file read instead
# of a ~16s build. That is what fills in a ripple/svelte draft's card, since the
# create-time capture deliberately refuses to build one. It goes by POCKET because a
# preview's return value is a transient doc nobody persists, and it is still not a
# way to photograph a live site: the resolved doc has a url, which the draft capture
# declines.
#
# Updated 2026-08-07 (SC-3 — the card stops lying after a republish). The
# deploy-time capture SC-1 added already fires on a republish, and the republish
# path was verified end to end rather than assumed: ``_deploy_site_doc`` upserts the
# EXISTING doc (so ``preview_image_url`` survives the republish and is overwritten,
# not reset), nothing short-circuits on a preview already being present, and each
# capture stores under a fresh uploads id — so the field takes a new value the card
# has never fetched, and no cache can serve the old bytes behind it. New
# ``refresh_site_preview`` adds the manual half of the policy: an explicit,
# synchronous re-capture that ROUTES itself (live page vs draft markup) the same way
# the automatic path does, and — unlike every deploy-triggered capture — reports
# failure to its caller. That asymmetry is the point: ``safe_take_*`` exists so a
# picture can never cost anyone a publish, while a person who pressed "refresh
# preview" needs to be told when it did not work rather than handed back the stale
# url they were trying to replace.
#
# Updated 2026-08-08 (feat/sites-js-by-default): ``publish_pocket`` is now the ONE
# place the tri-state ``keepsClientBundle`` collapses to a bool. The pocket field
# became ``bool | None``, so publish can finally tell "the author declared
# nothing" (``None`` — every legacy pocket) from an explicit ``False``. Undeclared
# resolves to the ``sites_keep_client_bundle_default`` setting, which ships TRUE:
# a Paw Site now keeps its own JavaScript by default. An explicit declaration
# still wins in BOTH directions, so a site that says ``False`` gets no bundle
# regardless of the setting. Everything downstream (``publish``,
# ``_deploy_site_doc``, ``generator.build``, the deferred-activation snapshot)
# still receives a plain resolved ``bool`` — no other signature moved. Note the
# cost this buys: the build-time resting-visibility smoke gate keys off whether
# the built artifact ships JS, so with the default on it stops firing for
# undeclared sites, and the "content hidden until JS reveals it" class of bug is
# no longer caught before deploy.
#
# Updated 2026-08-08 (a preview is never a photograph of a page that was not serving
# yet). ``refresh_site_preview`` gained the READINESS branch: a deploy is live at
# Cloudflare before it is live at the edge, so the capture path now polls the site's
# url before spending a render (see ``sites.screenshot`` — that module's header holds
# the full reasoning). This function runs the same gate on a SHORT budget, because
# unlike the deploy-time capture it has somebody waiting on it, and raises its own
# ``sites.preview_not_serving`` rather than reusing ``preview_unavailable``: the two
# declines need opposite advice ("publish it" vs "it is published, try again in a
# moment"), and the wrong one surfaces verbatim in the dashboard. This endpoint is
# also the recovery path when the deploy-time gate times out and deliberately leaves
# the card without a picture.
#
# Updated 2026-08-07 (MT-1 — an interactive site keeps its own JavaScript):
# ``publish_pocket`` now reads the pocket's ``keepsClientBundle`` declaration and
# threads it through ``publish`` -> ``_deploy_site_doc`` -> ``generator.build``, so a
# site whose hand-written client JS is load-bearing is generated with ``csr = true``
# instead of the static ``csr = false`` default (and the ripple prune step then leaves
# the hydration bundle alone). Two things make it survive the paths that historically
# lose per-site facts:
#   * It lives on the POCKET, not on the Site doc, so a republish — an
#     ``edit_svelte_component`` or a ``make_site_editable``, both of which route back
#     through ``publish_pocket`` — re-reads it rather than needing to carry it. That
#     is the failure mode ``builder_origin`` had to be taught to recover from.
#   * It is captured in ``pending_deploy_inputs`` alongside engine/source/pattern, so
#     the charge-first deferred deploy replays it at ``subscription.active``. That
#     snapshot IS the definition of what a deferred publish can reproduce; a field
#     missing from it is silently dropped, and a PAID interactive site would go live
#     with its JavaScript stripped. Pending docs written before this field read the
#     key as absent -> False -> the prior behaviour.
# Defaults False everywhere, so an ordinary static site is byte-identical.
#
# Updated 2026-08-02 (draft render): the PREVIEW deploy in ``publish`` is now
# engine-aware, closing the half of HE-4 that was missed. HE-4 taught the LIVE
# deploy that a built site's servable files live somewhere different per engine
# (``engines.static_output_rel``) but only touched ``_deploy_site_doc``; the DRAFT
# branch a few lines above kept calling ``deploy_local`` with no ``engine``, so an
# html draft resolved a SvelteKit build dir it never emits. The failure was
# invisible rather than loud, because ``deploy_local`` fails SOFT: with a prior
# deploy on disk it re-served THAT, so every edit-then-reload showed the previous
# page and nothing reported a problem. The live and draft halves of one function
# must resolve the static root the same way — that is the whole point of routing
# both through the shared predicate.
#
# Updated 2026-07-31 (provisioning brick): the dynamic single-flight guard is now
# BOUNDED. A job that was never consumed — no worker running — or that died
# without writing a terminal status used to leave the Site in
# ``provision_status="provisioning"`` forever, so every later publish of that
# pocket returned the in-progress no-op: an unpublishable pocket, HTTP 200, and
# no error anywhere to see. Sites stamp ``provision_started_at`` on entering the
# state and ``_provisioning_is_stale`` re-enqueues past the window.
#
# Updated 2026-07-31 (first-publish concierge): ``_embed_concierge_bar`` no longer
# guards provisioning on an existing Site doc. A FIRST publish reaches it before
# that doc is inserted, so the old guard skipped provisioning entirely and the
# page shipped bar-less with no log line — only a RE-publish grew a bar, which is
# exactly why the earlier fix read as working when it was verified that way.
#
# Updated 2026-07-30 (feat/paw-bar-autoembed): a published site now GROWS its own
# concierge. Until this, a site we generated with a concierge we auto-provisioned
# went live with nothing on the page: the bar was embedded only by a snippet the
# dashboard printed for a human to paste. ``_deploy_site_doc`` gains two steps,
# both on the LIVE path only (a preview publish returns from ``publish`` before it
# reaches either):
#   * ``_embed_concierge_bar`` — between the build and the deploy, write the embed
#     snippet into every built page (engine-aware root via ``static_output_rel``),
#     but only for a site that has earned one: concierge on, an embed key, a
#     paw-bar widget for the pocket, an agent bound to it. Idempotent (guarded on
#     the snippet's marker attribute) and failure-soft — an injection problem logs
#     and the site still deploys, because a site going live matters more than its
#     bar. The snippet, the marker and the gates live in ``paw_bar/embed.py``.
#   * ``_with_deployed_host`` — stamp the site's OWN deployed host onto
#     ``allowed_origins`` (both the insert and the update branch).
#     ``_default_allowed_origins`` seeds localhost only, so before this a visitor on
#     the real deployed host was refused by the very origin gate the bar and the
#     capture endpoint share. Additive, deduped, and never a wildcard or a
#     user-supplied host — only the URL we just deployed to.
#
# Updated 2026-07-23 (feat/site-dedicated-agent): added the public
# ``canonical_site_for_pocket(workspace_id, pocket_id)`` — a thin, tenant-scoped
# wrapper over the private ``_canonical_site_doc`` so the paw-bar concierge
# auto-provisioner resolves a pocket to its live Site through the SAME dedupe-aware
# logic (never reaching into a private helper). Read-only; no write-path change.
#
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): ``publish`` /
# ``_deploy_site_doc`` gain an OPTIONAL ``assets`` pass-through — the base64 binary
# sideband ({path: base64}) an html IMPORT sends alongside its text ``source`` map.
# It is forwarded to ``GeneratorClient.build`` ONLY when non-empty, so every
# existing publish path's build call stays byte-identical (cross-repo seam note in
# generator_client.py). ``_to_response`` also surfaces the new
# ``Site.import_report`` (None for non-imported sites). The import orchestration
# itself lives in the sibling ``import_service.py`` — this file only carries the
# sideband through the existing deploy chain.
#
# Updated 2026-07-17 (fix/sites-draft-visible — a DRAFT lists in the gallery):
# added ``create_draft_site`` — the create-site MCP handlers now mint ONE Site doc
# at CREATE time in a NOT-YET-DEPLOYED state so a draft-first pocket (pocketpaw#1744)
# lists in the /sites gallery immediately, instead of appearing nowhere until a
# publish first mints a Site doc. It is keyed on the SAME stable
# ``_id = _live_object_id(workspace, pocket)`` that publish upserts, so a later
# publish FINDS this draft and flips it in place (``deployed=True`` + url) rather
# than inserting a second doc — the PERF-1/PERF-2 one-doc-per-pocket invariant is
# preserved across create → publish. It is IDEMPOTENT (an existing doc — this draft
# or an already-live one — is returned untouched, never clobbered or duplicated),
# is NOT a deploy, and never touches billing (a draft opens no checkout / Dodo sub —
# only publish does). It seeds the SAME capture defaults publish's first-insert
# seeds (signed_key / allowed_origins / event_mapping) so a first publish taking the
# UPDATE branch over the draft keeps a working captureSignedKey + lead mapping. A
# draft reads draft/not-live everywhere because BP-2 keys ``is_live`` on
# ``doc.deployed`` (False here), never on doc-existence.
#
# Updated 2026-07-17 (fix/sites-prewarm-origin — pre-warm the origin a VIEW asks for):
# the native-artifact pre-warm must build with the SAME origin the browser's
# ``GET /native-artifact`` view resolves (the request Origin header), or the content
# hashes differ and the pre-warmed artifact is never hit — every view stays a cold
# miss. ``publish_pocket`` and ``apply_leaf_edits`` gained a ``prewarm_origin`` param
# the REST routers thread the request Origin into; it steers ONLY the pre-warm's armed
# artifact (``builder_origin=prewarm_origin or builder_origin``), never the PUBLIC
# deploy (a ``/sites/publish`` live deploy stays plain). Chat-agent / MCP callers pass
# no ``prewarm_origin`` (no request origin), so the pre-warm keeps its
# PAW_SITES_BUILDER_ORIGIN env fallback — set that env to the dashboard origin in
# deployments as a belt-and-braces default so the fallback matches the view too.
# ``edit_svelte_component`` is UNCHANGED: it is MCP-only (no request origin) and warms
# with the origin the site was armed/published with (``prior.builder_origin``), which
# already equals the dashboard origin a view asks for.
#
# Updated 2026-07-17 (feat/sites-native-artifact-no-build — kill preview-time builds):
# ``get_native_artifact`` is now a READ-THROUGH cache instead of building on every
# call. It hashes the pocket's render inputs (svelte source map + theme + builder
# origin + ``generator_client.generator_version()``) into a content hash and serves a
# prior ``{body_html, css}`` from the new filesystem artifact store
# (``_FilesystemArtifactStore`` → ``generator_client.artifact_home()``,
# ``~/.pocketpaw/site-artifacts/<pocket_id>/<hash>.json``) on a HIT — ZERO subprocess
# builds. A MISS builds once (armed, via the factored ``_build_native_artifact``),
# stores, and returns. Builds now happen only at publish and (pre-warmed) at edit-arm,
# never on a plain view (the prod box ran a 1-2 min SvelteKit build on every site
# view before this). A background pre-warm (``_prewarm_native_artifact`` →
# ``_safe_prewarm`` scheduled via ``_default_prewarm_scheduler``) repopulates the store
# after ``apply_leaf_edits`` / ``edit_svelte_component`` mutate source and after a LIVE
# svelte ``publish_pocket``, so the next view/arm is a hit; it is best-effort and never
# gates the mutation. ARMED-VS-PLAIN PUBLISH FINDING: the ``/sites/publish`` live
# deploy threads NO builder_origin (router calls ``publish_pocket`` without one), so the
# public deploy is PLAIN — no data-uid / no ``paw-edit-manifest`` on public pages; the
# armed artifact the native editor needs is produced by the pre-warm, NOT by shipping
# the edit-bridge to public pages. The ``_store`` seam keeps "where the render comes
# from" injectable so a later client-side-REPL compile wave can swap it cheaply.
#
# Updated 2026-07-14 (Paw Bar concierge seam, T1): added ``mint_foreign_site`` — a
# minimal Site writer for a FOREIGN origin (a site we did NOT generate). It creates
# a ``script_name=""`` / ``deployed=False`` Site that carries only the concierge
# credential (a freshly minted ``signed_key`` + normalized ``allowed_origins`` +
# ``scopes``); it is resolved by ``signed_key`` (via ``auth.site_keys.resolve_site_key``),
# not by ``script_name``, so the empty script name is fine. Kept HERE, not in the
# auth module, because this service is the sole owner of Site writes. Helper
# ``_normalize_origin_hosts`` reduces caller-supplied origins to the bare hosts
# ``origin_allowed`` matches on. Deliberately does NOT reuse ``_live_object_id`` (a
# foreign concierge must not collide with a published site's stable per-pocket id).
# Review follow-up (HIGH): ``mint_foreign_site`` now runs the pockets-service
# ownership check (``pockets_service.get(pocket_id, owner)``) BEFORE inserting, the
# same gate ``publish_pocket`` uses, so a caller cannot bind a concierge to another
# workspace's pocket (which would leak that pocket's KB to the resolved context).
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

import asyncio
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

from pocketpaw.sites_capture.contact_form import CONTACT_FORM_TYPE, default_event_mapping
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
from pocketpaw_ee.cloud.models.site import SiteInvoice as _SiteInvoiceDoc
from pocketpaw_ee.sites.build_state import claim_precondition
from pocketpaw_ee.sites.domain import HostnameStatus
from pocketpaw_ee.sites.dto import (
    AuditFinding,
    AuditResponse,
    DevPreviewResponse,
    DomainStatusResponse,
    SiteClientResponse,
    SiteClientUpdate,
    SiteDataRowsResponse,
    SiteDataTableInfo,
    SiteDataTablesResponse,
    SiteInvoiceCreate,
    SiteInvoiceOut,
    SitePreviewRefreshResponse,
    SitePreviewResponse,
    SiteResponse,
    SiteStatusResponse,
)
from pocketpaw_ee.sites.engines import content_key, is_source_engine, normalize_engine
from pocketpaw_ee.sites.generator_client import BuildResult, GeneratorClient
from pocketpaw_ee.sites.html_paths import (
    html_path_rejection,
    is_reserved_html_path,
    normalize_html_path,
)
from pocketpaw_ee.sites.react_paths import is_reserved_react_path, react_path_rejection

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


# ---------------------------------------------------------------------------
# Native-artifact read-through cache (feat/sites-native-artifact-no-build).
#
# Viewing a svelte Paw Site in the native editor USED to run a full SvelteKit build
# on every ``GET /native-artifact`` call — fine on a fast laptop, 1-2 min on the prod
# Hetzner box. The cache below makes a VIEW a disk read: get_native_artifact hashes
# the pocket's current render inputs and serves a prior ``{body_html, css}`` from disk
# on a hit; a miss builds once, stores, and returns. Builds now happen only at publish
# and (pre-warmed in the background, then cached) at edit-arm — never on a plain view.
# The artifact SOURCE stays behind one seam (the store) so a later "compile in the
# client-side REPL" wave can swap where the render comes from without touching callers.
# ---------------------------------------------------------------------------


def _artifact_content_hash(
    *, source: dict[str, Any], theme: dict[str, Any], builder_origin: str, gen_version: str
) -> str:
    """Fingerprint the inputs that determine a native artifact's rendered output — the
    svelte source map, the theme, the builder origin (it changes the stamped
    data-uid + edit-bridge), and the generator version (a toolchain/dep bump changes
    the built HTML/CSS). A stable hash for an unchanged render; it changes the moment
    any input does, which is exactly when the cached artifact is stale and a rebuild is
    required. The store is already keyed per pocket by path, so this need only separate
    an unchanged render from a changed one within a pocket."""
    import hashlib

    h = hashlib.sha256()
    for part in (
        gen_version,
        builder_origin or "",
        json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        json.dumps(theme, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    ):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


# Keep at most this many artifact files per pocket (current + previous by default), so
# the store never grows unbounded as a pocket is edited. Override PAW_SITES_ARTIFACT_KEEP.
_ARTIFACT_KEEP_DEFAULT = 2


def _artifact_keep() -> int:
    import os

    raw = os.environ.get("PAW_SITES_ARTIFACT_KEEP")
    try:
        n = int(raw) if raw else _ARTIFACT_KEEP_DEFAULT
    except (TypeError, ValueError):
        return _ARTIFACT_KEEP_DEFAULT
    return n if n > 0 else _ARTIFACT_KEEP_DEFAULT


class _FilesystemArtifactStore:
    """Default read-through store for get_native_artifact — persists a rendered
    ``{body_html, css}`` as a JSON file at ``artifact_home()/<pocket_id>/<hash>.json``.

    Tenant isolation: the path is keyed on ``pocket_id`` (resolved from a
    tenant-scoped pockets read), so one tenant's store dir is never addressable from
    another tenant's request. Reads and writes are best-effort — a missing / corrupt /
    partial file reads as a MISS (return ``None``) so a bad entry degrades to a rebuild
    rather than surfacing an error, and a failed write is swallowed (the render still
    returns, just uncached). This class is the injection seam (the ``_store`` param) so
    tests exercise the read-through logic with an in-memory fake and never touch disk."""

    def read(self, pocket_id: str, content_hash: str) -> tuple[str, str] | None:
        from pocketpaw_ee.sites.generator_client import artifact_home

        path = artifact_home() / pocket_id / f"{content_hash}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        try:
            data = json.loads(raw)
            body_html = data["body_html"]
            css = data["css"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(body_html, str) or not isinstance(css, str):
            return None
        return body_html, css

    def write(self, pocket_id: str, content_hash: str, body_html: str, css: str) -> None:
        import os
        import tempfile

        from pocketpaw_ee.sites.generator_client import artifact_home

        pocket_dir = artifact_home() / pocket_id
        try:
            pocket_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    "body_html": body_html,
                    "css": css,
                    "stored_at": datetime.now(UTC).isoformat(),
                }
            )
            # Atomic write: a partial file must never read as a hit. Write a temp file in
            # the same dir, then os.replace (atomic on the same filesystem).
            fd, tmp = tempfile.mkstemp(dir=str(pocket_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, str(pocket_dir / f"{content_hash}.json"))
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            # Best-effort cache — a write failure must not break the render path.
            logger.warning(
                "sites.artifact_store: write failed for pocket %s", pocket_id, exc_info=True
            )
            return
        self._evict(pocket_dir)

    def _evict(self, pocket_dir: Path) -> None:
        """Keep only the newest ``_artifact_keep()`` artifact files (current + previous
        by default) in the pocket dir, deleting the oldest by mtime. Best-effort."""
        try:
            files = sorted(
                (p for p in pocket_dir.glob("*.json") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in files[_artifact_keep() :]:
            try:
                stale.unlink()
            except OSError:
                pass


_DEFAULT_ARTIFACT_STORE = _FilesystemArtifactStore()


def _default_artifact_store() -> _FilesystemArtifactStore:
    """The process-wide default native-artifact store (a filesystem store). Factored so
    ``get_native_artifact`` / the pre-warm resolve the same instance and tests can pass
    an in-memory ``_store`` instead."""
    return _DEFAULT_ARTIFACT_STORE


async def _build_native_artifact(
    *,
    generator: GeneratorClient,
    theme: dict[str, Any],
    source: dict[str, Any],
    site_name: str,
    builder_origin: str,
    pocket_id: str,
    read: Callable[[str], tuple[str, str]],
) -> tuple[str, str]:
    """Run the ARMED svelte build for a native artifact and extract ``(body_html, css)``.

    The single build path shared by get_native_artifact's cache MISS and the
    background pre-warm. It builds with ``builder_origin`` set (so the paw-sites
    generator stamps ``data-uid`` on the editable leaves + embeds the
    ``paw-edit-manifest``) through ``_build_or_cloud_error`` (a toolchain / non-zero /
    SmokeGate failure becomes a clean CloudError, not an opaque 500), then reads the
    built ``<body>`` inner HTML + concatenated CSS off disk. ``smoke=False`` is the
    arm/preview gate — skip the SSR fail-check but still emit the static output. A
    svelte pocket has no rippleSpec, so ``ripple_spec={}`` (mirrors the prior inline
    call); PERF-3's stable per-pocket build dir keeps node_modules / bun install
    cached across builds so the arm build reuses the publish build's install."""
    build = await _build_or_cloud_error(
        generator,
        ripple_spec={},
        theme=theme,
        # Transient, per-pocket-stable id — cosmetic here (only rides the built page's
        # capture config, which the native editor ignores). The build DIR is keyed on
        # pocket_id (PERF-3), not this, so it does not affect where the output lands.
        site_id=_preview_id(pocket_id),
        title=site_name,
        capture_api_base=_capture_base(),
        capture_signed_key=f"site_key_{secrets.token_urlsafe(24)}",
        engine="svelte",
        source=source,
        builder_origin=builder_origin,
        pocket_id=pocket_id,
        smoke=False,
    )
    return read(build.project_dir)


async def _prewarm_native_artifact(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    builder_origin: str | None = None,
    _generator: GeneratorClient | None = None,
    _read_built: Callable[[str], tuple[str, str]] | None = None,
    _store: Any | None = None,
) -> None:
    """Produce + cache the ARMED native artifact for a pocket in the BACKGROUND so the
    next preview/arm is a read-through cache HIT instead of an on-interaction build.

    Fired after a source mutation (leaf edit / component edit) and after a LIVE svelte
    publish. It re-reads the pocket (source is the source of truth and may have just
    changed), computes the SAME content hash ``get_native_artifact`` uses, and — only
    if the store does not already hold that render — builds once and stores it. So a
    mutation that lands identical source, or a re-publish of unchanged source, rebuilds
    nothing.

    Best-effort by contract: callers schedule it through ``_safe_prewarm`` (which
    swallows + logs) so the edit / publish they own returns regardless of a pre-warm
    failure (missing toolchain, a non-svelte pocket, a read error)."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import generator_client

    pocket = await pockets_service.get(pocket_id, user_id)
    # Only svelte sites have a native shadow-render build; ripple/html don't use this
    # path (an html site's served artifact IS its source). Nothing to arm otherwise.
    if (pocket.get("engine") or "ripple") != "svelte" or not isinstance(pocket.get("source"), dict):
        return
    source = pocket["source"]
    ripple_spec = pocket.get("rippleSpec") or {}
    theme = (ripple_spec.get("theme") if isinstance(ripple_spec, dict) else {}) or {}
    site_name = (pocket.get("name") or "").strip() or "Untitled site"
    origin = (builder_origin or "").strip() or _builder_origin()

    store = _store or _default_artifact_store()
    content_hash = _artifact_content_hash(
        source=source,
        theme=theme,
        builder_origin=origin,
        gen_version=generator_client.generator_version(),
    )
    if store.read(pocket_id, content_hash) is not None:
        return  # already warm — no rebuild
    generator = _generator or GeneratorClient()
    read = _read_built or _read_native_artifact
    body_html, css = await _build_native_artifact(
        generator=generator,
        theme=theme,
        source=source,
        site_name=site_name,
        builder_origin=origin,
        pocket_id=pocket_id,
        read=read,
    )
    store.write(pocket_id, content_hash, body_html, css)


async def _safe_prewarm(**kwargs: Any) -> None:
    """Run ``_prewarm_native_artifact`` swallowing ALL errors — pre-warm is best-effort
    and must never break the edit / publish that scheduled it."""
    try:
        await _prewarm_native_artifact(**kwargs)
    except Exception:  # noqa: BLE001 — pre-warm is best-effort, never a gate
        logger.warning(
            "sites.prewarm: native-artifact pre-warm failed for pocket %s",
            kwargs.get("pocket_id"),
            exc_info=True,
        )


# Background-task keepalive: asyncio holds only a WEAK ref to a bare create_task, so a
# fire-and-forget task can be garbage-collected mid-run. Hold a strong ref until done.
_PREWARM_TASKS: set[asyncio.Task[None]] = set()


def _default_prewarm_scheduler(coro: Any) -> None:
    """The production pre-warm scheduler — detach the coroutine as a background task on
    the running loop and return immediately (off the caller's critical path). With no
    running loop (a sync call site), close the coroutine and skip; pre-warm is
    best-effort. Tests patch this module attr to capture the coroutine instead."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _PREWARM_TASKS.add(task)
    task.add_done_callback(_PREWARM_TASKS.discard)


def _schedule_native_prewarm(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    builder_origin: str | None = None,
    _generator: GeneratorClient | None = None,
    _read_built: Callable[[str], tuple[str, str]] | None = None,
    _store: Any | None = None,
) -> None:
    """Fire a background native-artifact pre-warm for a pocket. A thin, non-async
    wrapper the mutation / publish call sites invoke; it never blocks or raises. The
    injected ``_generator`` seam is forwarded so a faked-generator publish/edit
    pre-warms with the SAME fake (unit tests never shell out). The scheduler is looked
    up on the module (``_default_prewarm_scheduler``) so tests can patch it."""
    _default_prewarm_scheduler(
        _safe_prewarm(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            builder_origin=builder_origin,
            _generator=_generator,
            _read_built=_read_built,
            _store=_store,
        )
    )


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
#
# Updated 2026-08-13: both this and the mapping below are now DERIVED from
# ``sites_capture.contact_form``, the single declaration of what a contact form
# is. They used to be restated here, and the restatement drifted from the form
# ``landing_assembler`` actually generates — the assembler emitted ``name="name"``
# while this mapping read ``{{ payload.full_name }}``, so every lead captured
# through the deterministic landing path stored an empty name and threw away the
# value the visitor typed. Nothing compared the two files, so nothing caught it.
# Derivation is the fix: a field rename is now one edit in one tuple.
_DEFAULT_FORM_TYPE = CONTACT_FORM_TYPE

# Default event mapping seeded at publish so a basic contact lead lands with NO
# manual Mongo edit. The interpolator drops any ``{{ payload.X }}`` whose key is
# absent from the submission (resolves to None), so a form that only sends
# {full_name, phone} still produces a Lead — the extra fields come back empty.
_DEFAULT_EVENT_MAPPING: dict[str, Any] = default_event_mapping()


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
        # The name a customer pastes at their own registrar. NOT defaulted here: it
        # must be a proxied record on our zone, only the operator knows which one, and
        # the value this used to derive from the zone id had no DNS records at all.
        # ``create_custom_hostname`` refuses when it is unset — see cloudflare_client.
        cname_target=os.environ.get("PAW_CF_CNAME_TARGET", ""),
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
        # SI-4: the persisted import summary for an imported site; None for every
        # non-imported site (empty dict on the doc reads as None on the wire).
        import_report=getattr(doc, "import_report", None) or None,
        # SC-1: the screenshot of the site's live page the gallery card renders.
        # None until a capture lands (empty string on the doc, and every pre-SC-1
        # row via the getattr default, read as None on the wire).
        preview_image_url=getattr(doc, "preview_image_url", "") or None,
        # SL-3: the build lane's state, straight off the persisted row. These three
        # were declared on the DTO by SG-9i and never populated here, so every
        # response carried the DEFAULTS — ``build_status`` frozen at "none" no matter
        # what the row said. A client polling a build therefore watched a field that
        # could not change, which is indistinguishable from a build that never starts.
        #
        # Read with ``getattr`` defaults like every field above, so a pre-SG-9i row
        # reads as "no build" rather than raising.
        #
        # ``build_status`` is passed through VERBATIM — never normalised against a
        # known set. The wire's contract is that a client treats an unrecognised status
        # as in-progress, and mapping an unknown value to "none" here would break that
        # from the server side: it would tell a client "nothing is building" about a
        # build that is running under a status this deploy predates.
        build_status=getattr(doc, "build_status", "none"),
        build_reason=getattr(doc, "build_reason", None),
        build_job_id=getattr(doc, "build_job_id", None),
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


async def _emit_site_created(doc: _SiteDoc) -> None:
    """Publish ``SiteCreated`` for a freshly minted DRAFT Site doc.

    Deliberately UNGUARDED — the caller owns the try/except, so patching this in a
    test exercises that guard rather than replacing it. Lazy imports keep the sites
    service free of a hard cloud-realtime dependency, matching ``publish_pocket``.
    """
    from pocketpaw_ee.cloud._core.realtime.emit import emit
    from pocketpaw_ee.cloud._core.realtime.events import SiteCreated

    await emit(
        SiteCreated(
            data={
                "workspace_id": doc.workspace,
                "site_id": str(doc.id),
                "pocket_id": doc.pocket_id,
                "owner": doc.owner,
                # Always False on a fresh draft. Sent anyway so a listener can tell a
                # draft from a publish off the payload alone, with no second read.
                "deployed": False,
            }
        )
    )


async def create_draft_site(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    name: str = "",
) -> _SiteDoc:
    """Mint the DRAFT Site doc for a freshly created site pocket so it lists in the
    /sites gallery BEFORE it is ever published (fix/sites-draft-visible).

    Draft-first create (pocketpaw#1744) persists a site POCKET but no Site doc — Site
    docs were only minted at PUBLISH — so a draft appeared in neither the gallery's
    All nor its Draft filter (``list_for_workspace`` reads Site docs, and a draft had
    none). This mints ONE canonical Site doc in a NOT-YET-DEPLOYED state so the draft
    lists naturally and reads as a draft everywhere (``pocket_status`` and the card
    badge key on ``deployed``, not on doc-existence — BP-2), while a plain create still
    does NOT deploy.

    Dedupe invariant (PERF-1/PERF-2): the doc is keyed on the SAME stable
    ``_id = _live_object_id(workspace_id, pocket_id)`` that ``publish`` upserts, so a
    later publish FINDS this draft and flips it in place (``deployed=True`` + url)
    instead of minting a second doc — exactly ONE Site doc per pocket across
    create → publish. IDEMPOTENT: if a Site doc already exists for the pocket (this
    draft on a duplicate create, or an already-published/live one), it is returned
    UNCHANGED — the mint never clobbers a live doc back to draft and never duplicates.

    NOT a deploy and NOT a billing event: a draft never builds, never contacts
    Cloudflare, and never opens a checkout / Dodo subscription (only ``publish`` does).
    It seeds the SAME capture defaults ``publish``'s first-insert seeds — a minted
    ``signed_key``, ``allowed_origins``, ``event_mapping`` — so when publish later takes
    the UPDATE branch over this draft (which preserves those fields), the built page's
    ``captureSignedKey`` matches the persisted doc and lead capture works on the first
    publish. ``publish`` reuses a stored non-empty ``signed_key``, so minting one here is
    what keeps the built key and the doc in sync (the same invariant
    ``test_republish_reuses_signed_key`` pins for a re-publish).

    REALTIME (fix/sites-draft-realtime): a FRESH mint emits ``SiteCreated`` so an
    already-open gallery gains the card on its own. This used to carry a
    ``# no-event`` opt-out reasoning that nothing downstream keys on a draft doc —
    true of the search index and soul memory, but the gallery is a listener too, and
    it was left with no signal at all. The per-run ``pocket_created`` SSE does not
    cover this: it reaches only the tab that owns that chat stream, so a draft
    created from /chat, a second tab, a teammate's session, or a zip/url import was
    invisible until someone pressed Refresh. The emit sits AFTER the idempotent early
    return above, so it fires once per real insert — a repeat create, or one against
    an already-live doc, stays silent.
    """
    oid = _live_object_id(workspace_id, pocket_id)
    # Idempotent + dedupe-safe: never mint a second doc for a pocket, and never reset
    # an already-published/live doc back to draft. If a doc already exists (this draft
    # on a repeat create, or a live one), return it untouched.
    existing = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if existing is not None:
        return existing

    doc = _SiteDoc(
        id=oid,
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner=user_id,
        name=name.strip() if name else "",
        # Not deployed yet — a plain create stops at the draft. publish stamps
        # script_name (== the stable id) / url / deployed_at when it goes live.
        script_name="",
        deployed=False,
        url="",
        builder_origin="",
        # Seed the capture config publish's first-insert seeds so a first publish
        # (which finds this draft and takes the UPDATE branch, preserving these
        # fields) keeps a working signed_key + lead mapping. Minting the key here is
        # also what publish reuses, so the built captureSignedKey matches the doc.
        signed_key=f"site_key_{secrets.token_urlsafe(24)}",
        allowed_origins=_default_allowed_origins(),
        event_mapping=_DEFAULT_EVENT_MAPPING,
    )
    await doc.insert()
    # The gallery IS a downstream listener, and a draft is the moment its card
    # appears — so this emits, where it used to carry a ``# no-event`` opt-out.
    # Guarded like every other post-insert side effect below: the Site doc is the
    # primary contract, and a realtime failure must never cost the user the site
    # they just asked for. ``emit`` already swallows publish errors; this also
    # catches the "no bus initialised" AssertionError, so a create that runs outside
    # a booted cloud (a script, a test tree without the bus fixture) still succeeds.
    try:
        await _emit_site_created(doc)
    except Exception:  # noqa: BLE001
        logger.warning(
            "create_draft_site: site.created emit failed (non-fatal) for pocket %s",
            pocket_id,
            exc_info=True,
        )
    # SC-2: this is the moment the draft becomes a card in the gallery, so it is the
    # moment to try to give that card a picture. A draft has no url, so the capture
    # shoots its MARKUP instead. Only on a fresh mint — the idempotent early return
    # above means a repeat create never re-shoots, and a live doc never gets a draft
    # picture. Wrapped like every other publish-tail side effect: a create must not
    # fail because a thumbnail could not be taken.
    _schedule_draft_screenshot(doc)
    return doc


def _normalize_origin_hosts(origins: list[str]) -> list[str]:
    """Reduce a list of origins to the bare, lowercased HOSTS ``origin_allowed``
    matches against (T1). ``origin_allowed`` strips scheme/port/path off the
    INBOUND ``Origin`` header and then tests bare-host membership in the stored
    list, so the stored list must itself be bare hosts — otherwise a caller who
    passes ``https://brewco.com:443`` would store a value the runtime match can
    never hit. Dedupes, preserves order, drops empties."""
    hosts: list[str] = []
    for origin in origins:
        host = origin.strip().lower()
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.split("/", 1)[0].split(":", 1)[0]
        if host and host not in hosts:
            hosts.append(host)
    return hosts


async def mint_foreign_site(
    *,
    workspace_id: str,
    pocket_id: str,
    owner: str,
    allowed_origins: list[str],
    name: str = "",
    scopes: list[str] | None = None,
) -> _SiteDoc:
    """Mint a Site for a FOREIGN origin — one PocketPaw did not generate (T1).

    A normal Site is created by ``publish`` for a pocket we render into a Worker;
    its ``script_name`` is the deployed Worker id and it is looked up by that id.
    A Paw Bar concierge instead embeds on a site the customer already owns (a
    Squarespace page, a hand-rolled marketing site, …). There is no Worker to
    deploy, so this mints a Site with ``script_name=""`` and ``deployed=False``
    whose ONLY job is to carry the concierge credential: the world-visible
    ``signed_key`` (minted here, same ``site_key_...`` format ``publish`` seeds),
    the ``allowed_origins`` the embed is valid from, and the ``scopes`` a resolved
    request may exercise. It is resolved not by ``script_name`` (empty) but by that
    ``signed_key`` — ``auth.site_keys.resolve_site_key`` does the key→Site lookup —
    so an empty ``script_name`` is not a problem.

    Site writes are owned by this service (the sole Site writer), which is why the
    mint lives here rather than in the auth module that reads the key back.

    v1 mints a FRESH doc per call (fresh ObjectId, fresh key). It deliberately does
    NOT reuse ``_live_object_id`` — that derives a stable per-(workspace, pocket)
    id for a PUBLISHED site, and a foreign concierge for the same pocket must not
    collide with (or overwrite) a real published Worker doc. Idempotent binding
    management (one canonical concierge per pocket, rotate/rebind) is a follow-up
    (the pilot-bind slice); this primitive just creates the credential row.

    Args:
        workspace_id: Owning tenant (the Site's ``workspace``).
        pocket_id: The pocket the concierge is grounded in (drives the KB scope
            ``pocket:<pocket_id>`` downstream).
        owner: The acting user. Recorded as the Site's ``owner`` AND used as the
            identity for the pocket ownership check below — so it must be a user
            who can access ``pocket_id``, not an arbitrary label.
        allowed_origins: Origins the embed is valid from; normalized to bare hosts.
        name: Optional display name.
        scopes: Optional override of what the key may do; defaults to the Site
            model's concierge baseline when omitted.

    Returns:
        The inserted ``Site`` doc, carrying its freshly-minted ``signed_key`` so the
        caller can hand the embed snippet back to the owner.

    Raises:
        Forbidden: ``pocket.access_denied`` when ``owner`` cannot access
            ``pocket_id`` (via the pockets service ownership check).
        NotFound: when ``pocket_id`` does not exist.
    """
    # Ownership gate — the SAME check every other pocket-touching path in this
    # service runs (see ``publish_pocket`` → ``pockets_service.get``). Without it a
    # caller could mint a concierge bound to ANOTHER workspace's pocket, and the
    # resolved CONCIERGE context would then read that victim pocket's KB
    # (``pocket:<pocket_id>``). Run it BEFORE minting the key / inserting the doc so
    # a denied caller leaves no orphan Site behind. ``get`` raises
    # Forbidden("pocket.access_denied") on cross-tenant access and NotFound when the
    # pocket is missing; we only need it for the side-effect of that check.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    await pockets_service.get(pocket_id, owner)

    site = _SiteDoc(
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner=owner,
        name=name,
        script_name="",
        deployed=False,
        url="",
        allowed_origins=_normalize_origin_hosts(allowed_origins),
        signed_key=f"site_key_{secrets.token_urlsafe(24)}",
    )
    # Only override the model's default scope set when the caller asked to narrow
    # it, so the default stays the single source of truth.
    if scopes is not None:
        site.scopes = scopes
    await site.insert()
    return site


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
    assets: dict[str, str] | None = None,
    pattern: str | None = None,
    builder_origin: str | None = None,
    keeps_client_bundle: bool = False,
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
            keeps_client_bundle=keeps_client_bundle,
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
        # HE-4 parity with the LIVE deploy below: WHERE the built draft's servable
        # files sit is a per-engine fact (``static_output_rel``) — the SvelteKit
        # adapter output for ripple/svelte, the project root for html — so the real
        # deploy must be told the engine. Without it an html draft resolved
        # ``.svelte-kit/cloudflare``, which an html build never emits, and
        # ``deploy_local``'s fail-soft branch re-served the PRIOR deploy: the draft
        # showed the previous page and nothing surfaced an error. The injected test
        # seam stays 2-arg (a fake serves no real tree, so it ignores the engine),
        # exactly as ``_deploy_site_doc`` splits it.
        if _local_deploy is not None:
            preview_url = _local_deploy(preview_id, build.project_dir)
        else:
            preview_url = local_server.deploy_local(preview_id, build.project_dir, engine=engine)
        # SECOND, UNFIXED half of the draft-render bug — surfaced, not silenced.
        # Unlike the live deploy below, this path has NO deploy-mode fork: it
        # always serves from ``local_server``, which binds 127.0.0.1 inside THIS
        # process. On a dev box that is also the operator's machine, so the draft
        # frames fine and every local test passes. In a real deployment the browser
        # is somewhere else entirely and the URL is unreachable — the draft renders
        # broken in production for a reason no log ever mentioned. Giving the draft
        # a reachable URL off-box means deploying a preview worker per pocket
        # (cost, naming, teardown), which is a product call, not a bug fix. Until
        # that call is made, say so loudly at the moment we hand back the address.
        if not _local_mode() or _deploy_mode() not in (None, "local"):
            logger.warning(
                "sites: draft preview for pocket %s is served from this process's "
                "loopback (%s) — a browser off this host CANNOT load it, so the "
                "draft will render broken. The preview path has no workers/wfp "
                "deploy target yet (the live path does).",
                pocket_id,
                preview_url,
            )
        # SC-2: this build just put the pocket's current markup on disk, so a draft
        # capture here costs a file read rather than the 16s build the create-time
        # capture declines to spend — this is what fills in a ripple/svelte draft's
        # card at all. By POCKET, not by this object: a preview returns a transient,
        # never-persisted doc with nothing to record a picture on, while the real
        # draft doc is in Mongo under the stable per-pocket id. An already-LIVE site
        # resolves a doc WITH a url, which the draft capture declines — so previewing
        # a live site can never replace the picture of the page visitors see with a
        # picture of an unapproved edit.
        _schedule_draft_screenshot_for_pocket(workspace_id=workspace_id, pocket_id=pocket_id)
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
        assets=assets,
        pattern=pattern,
        builder_origin=builder_origin,
        keeps_client_bundle=keeps_client_bundle,
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
    assets: dict[str, str] | None = None,
    pattern: str | None = None,
    builder_origin: str | None = None,
    keeps_client_bundle: bool = False,
    generator: GeneratorClient | None = None,
    cloudflare: Any | None = None,
    bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
    local_deploy: Callable[[str, str], str] | None = None,
    # HE-4: the workers deployer is engine-aware (``deploy_workers(site_id,
    # project_dir, *, engine=..., d1_database_id=...)``), so the seam takes kwargs.
    workers_deploy: Callable[..., Any] | None = None,
    # SL-3: an ALREADY-BUILT project dir. When given, the inline build is SKIPPED and
    # everything after it (concierge embed → deploy → upsert → sync/screenshot) runs
    # verbatim. This is how the ephemeral build lane finishes a publish: the worker
    # built in a sandbox, materialised the artifact, and calls back in here rather than
    # growing a second copy of the deploy tail. One deploy path, two places the build
    # can have happened.
    prebuilt_project_dir: str | None = None,
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

    # SL-3: fork to the EPHEMERAL BUILD LANE for the engines whose artifact can
    # actually be deployed from it. A prebuilt dir means the worker already ran this
    # build and is calling back in to finish the publish, so it must NOT re-fork.
    if prebuilt_project_dir is None and build_runs_async(engine):
        return await _enqueue_static_build(
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
            assets=assets,
            pattern=pattern,
            builder_origin=builder_origin,
            keeps_client_bundle=keeps_client_bundle,
        )

    gen = generator or GeneratorClient()
    # DEP-3: map a generator/install/smoke failure (missing toolchain, non-zero
    # build, SSR fail-gate) to a clean CloudError (sites.generator_failed → 5xx)
    # instead of letting a bare FileNotFoundError / RuntimeError / SmokeGateFailed
    # escape as an unhandled 500. A misconfigured image (no paw-sites-gen / bun on
    # PATH) is the bug this guards.
    # SI-4: forward the html import's binary sideband ONLY when present, so every
    # non-import publish's build call (and every injected fake's expected kwargs)
    # stays byte-identical to before the assets seam existed.
    _asset_kwargs: dict[str, Any] = {"assets": assets} if assets else {}
    if prebuilt_project_dir is not None:
        # SL-3: the worker already built this site in a sandbox and materialised the
        # artifact on disk. Skip the build and reuse everything below it verbatim —
        # ``BuildResult`` carries only what the tail reads (``project_dir``), and the
        # SSR smoke gate does not apply because this artifact came back from a build
        # that already ran the engine's own gate inside the sandbox.
        build = BuildResult(project_dir=prebuilt_project_dir, ripple_version=None)
    else:
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
            keeps_client_bundle=keeps_client_bundle,
            **_asset_kwargs,
            # PERF-3: build into the STABLE per-pocket working dir so node_modules
            # persists and `bun install` is cached across builds, cutting the dominant
            # per-edit cost.
            pocket_id=pocket_id,
            # A live deploy keeps the SSR fail-gate (smoke=True) so the gate + the
            # rollback in edit_svelte_component are unchanged — a broken edit never
            # reaches the live deploy.
            smoke=True,
        )

    # Grow the concierge onto the built pages BEFORE they deploy, so the artifact
    # that goes live already carries the bar. This is a LIVE-publish-only step: a
    # preview returns from ``publish`` long before it reaches here, so a draft never
    # gets an embedded bar pointed at the live key. Failure-soft inside.
    await _embed_concierge_bar(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key=signed_key,
        project_dir=build.project_dir,
        engine=engine,
        # A FIRST publish has no Site doc yet — it is inserted further down — so
        # pass the two fields provisioning needs to stand one up in memory.
        user_id=user_id,
        site_name=site_name,
    )

    # Stamp the free-tier attribution badge onto the same built tree, also before
    # the deploy. Ordered AFTER the concierge so the bar is present when the badge
    # walks the pages (both are idempotent, so the order only decides which one
    # logs the rewrite — but a fixed order keeps re-publishes byte-stable).
    #
    # NOT failure-soft, unlike the concierge above: this raises and the publish
    # aborts rather than deploying an unbadged free site. See ``_stamp_free_badge``.
    await _stamp_free_badge(
        workspace_id=workspace_id,
        site_id=site_id,
        project_dir=build.project_dir,
        engine=engine,
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

        # HE-4: the real local deploy is engine-aware too — an html site's static
        # tree is the project root, not .svelte-kit/cloudflare. The injected test
        # seam stays 2-arg (fakes don't serve a real tree, so they ignore engine).
        if local_deploy is not None:
            url = local_deploy(site_id, build.project_dir)
        else:
            url = local_server.deploy_local(site_id, build.project_dir, engine=engine)
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
        # HE-4 / RX-1: pass the engine so an html OR react site deploys as an
        # assets-only Worker (no server script — react builds, but to a prerendered
        # static dist/), while ripple/svelte keep the SvelteKit-worker config.
        url = await deploy_w(site_id, build.project_dir, engine=engine)
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
            # Record WHICH target this deploy used, not which one was configured. The
            # custom-domain lane needs "does this site have its own route-addressable
            # Worker", and PAW_CF_DEPLOY_MODE cannot answer it at request time.
            deploy_target=mode,
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
            # production hostname when a custom domain is connected. The site's OWN
            # deployed host goes on too — without it a visitor on the real page is
            # refused by the origin gate the bar and the capture endpoint both run.
            allowed_origins=_with_deployed_host(_default_allowed_origins(), url),
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
        doc.deploy_target = mode
        doc.url = url
        # The deploy may have moved (local → workers, or a new sites domain), so
        # re-assert the site's own host on every publish. Idempotent and additive:
        # a host already present is not duplicated, and a custom domain appended by
        # ``add_domain`` is preserved (the list is only ever grown here).
        doc.allowed_origins = _with_deployed_host(doc.allowed_origins, url)
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

    # The site's content just changed, so the knowledge its concierge answers from
    # is now stale. Re-sync in the background: ingest compiles articles and can be
    # slow, and a publish must not wait on it — the site goes live immediately and
    # the concierge catches up a moment later. A preview publish never reaches here
    # (it returns earlier), so a draft never rewrites the live KB.
    _schedule_site_knowledge_sync(doc)
    # SC-1: the page the gallery card shows is now a different page, so re-shoot
    # it. Same placement and the same rule as the sync above — the site is
    # already live, so a screenshot may never fail or delay the publish. A
    # preview publish never reaches here, so a draft is never photographed.
    _schedule_site_screenshot(doc)
    return doc


async def _embed_concierge_bar(
    *,
    workspace_id: str,
    pocket_id: str,
    site_id: str,
    signed_key: str,
    project_dir: str,
    engine: str,
    user_id: str = "",
    site_name: str = "",
) -> None:
    """Write the concierge embed snippet into the built pages, before they deploy.

    A site we generated, with a concierge we auto-provisioned, used to ship with no
    concierge on it: the bar was embedded ONLY by a snippet the dashboard printed
    for a human to copy-paste, and nothing here ever wrote it. This is that missing
    step. It runs between the build and the deploy, so the artifact that goes live
    already carries the bar — no second deploy, no post-publish patch.

    ``concierge_enabled`` is read off the site's EXISTING doc, defaulting to True
    when there is none: this is a first publish, and the doc about to be inserted
    below carries the model's ``concierge_enabled=True`` default, so reading the
    absent doc as "on" is what makes a brand-new site behave like the one it is
    about to become rather than silently skipping its own first bar.

    FAILURE-SOFT, and that is the whole point of the try/except: this sits in the
    middle of a live publish. A site going live matters more than its bar, so an
    unreadable build tree, a store that will not answer, or anything else escaping
    here logs and lets the publish continue to deploy.
    """
    try:
        from pocketpaw_ee.paw_bar import embed
        from pocketpaw_ee.sites.engines import resolve_static_output_rel

        doc = await _SiteDoc.find_one({"_id": ObjectId(site_id), "workspace": workspace_id})
        concierge_enabled = True if doc is None else bool(doc.concierge_enabled)

        # Does this site's PLAN sell a concierge (feat/sites-concierge-entitlement)?
        # Resolved here rather than inside ``concierge_snippet`` because this is the
        # function that owns the Site doc — ``entitlements`` may not import
        # ``models.site`` (EE cloud rule 2), and ``embed`` has no business loading it.
        #
        # A no-op unless ``billing_enforced``: with billing off (OSS / self-host, and
        # every in-repo deploy today) this stays True and the publish path is byte
        # for byte what it was.
        #
        # Fail-closed on a FIRST publish, the same way the badge stamper does: no doc
        # means no ``plan_tier``, which resolves to the free floor and ships the page
        # bar-less. The opposite default would embed a bar on every brand-new site
        # regardless of plan, and that bar would 403 every visitor — a broken
        # concierge is worse than none. A republish after the subscription activates
        # picks it up, which is the same seam that repairs it for the badge.
        concierge_entitled = True
        from pocketpaw.config import get_settings

        if get_settings().billing_enforced:
            from pocketpaw_ee.cloud.entitlements import service as entitlements_service

            concierge_entitled = entitlements_service.resolve_site_entitlements(
                site_id=site_id,
                workspace_id=workspace_id,
                plan_tier=getattr(doc, "plan_tier", None),
                subscription_status=getattr(doc, "subscription_status", None),
                concierge_enabled=True,  # asking the PLAN; the switch is read above
            ).concierge_entitled

        # Publish-time provisioning (the third trigger): an agent-created site
        # published in the same conversation has passed through NEITHER
        # widget-create NOR a concierge-enable transition, so it reaches this
        # embed with no widget and no dedicated agent — and the four-gate
        # snippet check below would silently skip the bar. Mint the widget +
        # agent here so the first publish ships with its concierge. Idempotent
        # and failure-soft inside; requires the site doc (draft flows have one).
        if concierge_enabled:
            from pocketpaw_ee.paw_bar.agent_provisioning import ensure_site_widget

            # A FIRST publish reaches here BEFORE the Site doc is inserted, so
            # ``doc`` is None and the old ``doc is not None`` guard skipped
            # provisioning entirely: no widget, no dedicated agent, the
            # four-gate snippet check returned "" and the page shipped bar-less
            # — with no log line, because the empty snippet returns early. Only
            # a SECOND publish (doc now present) grew a bar, which is exactly
            # why this looked fixed. Stand up a transient doc for that first
            # pass: ``ensure_site_widget``/``ensure_site_agent`` only read
            # ``.workspace``/``.owner``/``.id``/``.name``/``.pocket_id`` off the
            # object, never re-reading the DB, and the real insert below carries
            # the same values.
            provisioning_doc = doc
            if provisioning_doc is None:
                provisioning_doc = _SiteDoc(
                    id=ObjectId(site_id),
                    workspace=workspace_id,
                    pocket_id=pocket_id,
                    owner=user_id,
                    name=site_name,
                    signed_key=signed_key,
                )
            await ensure_site_widget(provisioning_doc, workspace_id)

        snippet = await embed.concierge_snippet(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            site_key=signed_key,
            # The SAME base the generated capture endpoint posts leads to. Deriving
            # the loader URL from it (instead of a CDN constant) is what makes a
            # locally served site get a working localhost URL, and means there is
            # only one env var to move when the deploy moves.
            api_base=_capture_base(),
            concierge_enabled=concierge_enabled,
            concierge_entitled=concierge_entitled,
        )
        if not snippet:
            return

        # HE-4: where the deployable pages live differs by engine — the SvelteKit
        # adapter output for ripple and dynamic svelte, ``build`` for a STATIC svelte
        # site (SL-1, adapter-static), the project dir itself for html. Resolved off
        # the artifact because the svelte answer is a property of the site, not the
        # engine name — and injecting into the wrong root silently embeds nothing.
        root = Path(project_dir, resolve_static_output_rel(project_dir, engine))
        changed = embed.inject_into_tree(root, snippet)
        logger.info(
            "sites: embedded the concierge bar into %d page(s) of site %s",
            len(changed),
            site_id,
        )
    except Exception:  # noqa: BLE001 — a site going live matters more than its bar
        logger.warning(
            "sites: could not embed the concierge bar for site %s — publishing without it",
            site_id,
            exc_info=True,
        )


async def _stamp_free_badge(
    *,
    workspace_id: str,
    site_id: str,
    project_dir: str,
    engine: str,
) -> None:
    """Stamp the attribution badge onto the built pages, before they deploy.

    The sibling of ``_embed_concierge_bar`` — same seam (between build and deploy,
    so the artifact that lands is already right), same output-root resolution —
    with the OPPOSITE failure posture, which is the entire point.

    ``_embed_concierge_bar`` swallows everything because a site going live matters
    more than its bar. This one swallows NOTHING. The badge is what the paid
    per-site tier sells the removal of, so a badge that fails open is not an
    enforcement mechanism: the exploit is to make injection fail and keep the free
    unbadged site. ``BadgeInjectionError`` therefore propagates and the publish
    aborts before deploy — a site that cannot be badged does not ship.

    The site's billing fields are read off its EXISTING doc and resolved by
    ``entitlements.resolve_site_entitlements``, which is where the "may this site
    drop its badge" rule lives — NOT here, and not off ``plan_tier`` alone. A paid
    tier whose subscription is cancelled, pending, or was never charged at all
    keeps its ``plan_tier``, so reading the tier by itself hands those sites a
    free badge removal.

    A FIRST publish reaches here BEFORE the Site doc is inserted, exactly as the
    concierge embed above documents, so "no doc" must mean "free" rather than
    "skip" — the opposite default would ship every brand-new site unbadged, which
    is the bug this whole module exists to prevent.
    """
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service
    from pocketpaw_ee.sites import badge
    from pocketpaw_ee.sites.engines import resolve_static_output_rel

    doc = await _SiteDoc.find_one({"_id": ObjectId(site_id), "workspace": workspace_id})
    # ``getattr`` carries the no-doc case on its own: a first publish has no Site
    # row yet, and ``getattr(None, "plan_tier", None)`` is already the fail-closed
    # answer. The defaults here ARE the first-publish contract — an absent tier and
    # an absent subscription resolve to free-and-badged.
    ent = entitlements_service.resolve_site_entitlements(
        site_id=site_id,
        workspace_id=workspace_id,
        plan_tier=getattr(doc, "plan_tier", None),
        subscription_status=getattr(doc, "subscription_status", None),
        concierge_enabled=bool(getattr(doc, "concierge_enabled", True)),
    )

    if not ent.badge_required:
        logger.info(
            "sites: site %s is on paid tier %s with an active subscription — badge not required",
            site_id,
            ent.plan_tier,
        )
        return

    root = Path(project_dir, resolve_static_output_rel(project_dir, engine))
    changed = badge.inject_into_tree(root)
    logger.info(
        "sites: stamped the attribution badge onto %d page(s) of site %s",
        len(changed),
        site_id,
    )


def _with_deployed_host(allowed_origins: list[str], url: str) -> list[str]:
    """Ensure a site's OWN deployed host is on its capture/concierge allowlist.

    ``_default_allowed_origins`` seeds localhost only, so a site that deploys to a
    real host had a bar its own visitors were refused by: ``resolve_site_key``'s
    origin gate and the frame's ``frame-ancestors`` CSP both read this list, and
    both fail closed. The host is derived from the URL WE just deployed to — never
    from user input, never a wildcard — and appended only when missing, so a
    re-publish does not grow the list and a connected custom domain (appended by
    ``add_domain``) survives untouched.
    """
    host = _embed_deployed_host(url)
    if not host:
        return allowed_origins
    hosts = _normalize_origin_hosts(allowed_origins)
    if host not in hosts:
        hosts.append(host)
    return hosts


def _embed_deployed_host(url: str) -> str:
    """The bare host of a deployed URL. Thin indirection over ``embed.deployed_host``
    so the host-shape rule lives with the rest of the embed logic and this module
    keeps its lazy-import convention for reaching into paw_bar."""
    from pocketpaw_ee.paw_bar.embed import deployed_host

    return deployed_host(url)


def _schedule_site_knowledge_sync(site: _SiteDoc) -> None:
    """Fire the background site→pocket-KB sync. Non-async, never blocks, never
    raises. Looked up through the module so tests can patch it, mirroring
    ``_schedule_native_prewarm``.

    The try/except is the point: this is called from the tail of a LIVE deploy, so
    anything that escapes here would fail a publish of a site that is already
    deployed and serving. A concierge with stale knowledge is a much smaller problem
    than a publish that reports failure after succeeding.
    """
    try:
        from pocketpaw_ee.sites.kb_ingest import schedule_site_knowledge_sync

        schedule_site_knowledge_sync(site)
    except Exception:  # noqa: BLE001 — never fail a live publish over a KB sync
        logger.warning(
            "sites.kb: could not schedule knowledge sync for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


def _schedule_site_screenshot(site: _SiteDoc) -> None:
    """Fire the background screenshot of a freshly-deployed site (SC-1). Non-async,
    never blocks, never raises. Looked up through the module so tests can patch it,
    mirroring ``_schedule_site_knowledge_sync`` directly above.

    The try/except is the whole point, and it is a stronger requirement here than
    for the KB sync: this is called from the tail of a LIVE deploy, so anything
    escaping would fail a publish of a site that is already deployed and serving —
    and unlike a sync, the work behind it is a paid, quota'd, remote browser render
    that WILL time out or 400 sooner or later. A card with no picture is not a
    problem worth failing a publish over.
    """
    try:
        from pocketpaw_ee.sites.screenshot import schedule_site_screenshot

        schedule_site_screenshot(site)
    except Exception:  # noqa: BLE001 — never fail a live publish over a screenshot
        logger.warning(
            "sites.screenshot: could not schedule capture for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


def _schedule_draft_screenshot(site: _SiteDoc) -> None:
    """Fire the background screenshot of a freshly minted DRAFT site (SC-2). Non-async,
    never blocks, never raises. Looked up through the module so tests can patch it,
    mirroring ``_schedule_site_screenshot`` directly above.

    The try/except matters just as much here as on the live path, for a different
    reason: ``create_draft_site`` is called from the tail of a site CREATE and from
    the tail of a zip/from-url IMPORT, and the import call site does NOT wrap it. An
    escaping error would fail an import whose files are already safely persisted, in
    exchange for a picture on a gallery card.
    """
    try:
        from pocketpaw_ee.sites.screenshot import schedule_draft_screenshot

        schedule_draft_screenshot(site)
    except Exception:  # noqa: BLE001 — never fail a create over a thumbnail
        logger.warning(
            "sites.screenshot: could not schedule draft capture for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


def _schedule_draft_screenshot_for_pocket(*, workspace_id: str, pocket_id: str) -> None:
    """Fire the background draft capture for a pocket's Site doc (SC-2), from the tail
    of a PREVIEW build. Non-async, never blocks, never raises.

    By pocket rather than by doc because a preview's return value is transient and
    never persisted — the doc worth recording a picture on is the draft minted at
    create, which the capture resolves for itself. The try/except keeps a preview
    (the builder's inner loop, run on every edit) from ever failing over a thumbnail.
    """
    try:
        from pocketpaw_ee.sites.screenshot import schedule_draft_screenshot_for_pocket

        schedule_draft_screenshot_for_pocket(workspace_id=workspace_id, pocket_id=pocket_id)
    except Exception:  # noqa: BLE001 — never fail a preview over a thumbnail
        logger.warning(
            "sites.screenshot: could not schedule draft capture for pocket %s",
            pocket_id,
            exc_info=True,
        )


async def refresh_site_preview(*, workspace_id: str, site_id: str) -> SitePreviewRefreshResponse:
    """Re-capture a site's card image NOW, and report what happened (SC-3).

    The manual half of the preview policy. Automatic capture fires on every
    successful deploy, which covers the case that matters (the design changed), but
    it cannot cover a capture that FAILED — Cloudflare unconfigured at the time,
    quota exhausted, a render that timed out — or a draft whose markup only became
    buildable after it was minted. Without this the only way to fix a card was to
    republish an unchanged site.

    Deliberately the mirror image of the deploy path on the one axis that matters:
    **this one raises.** ``safe_take_*`` exists so a picture can never cost anybody
    a publish; here a person pressed a button and is waiting, so a Cloudflare
    failure must reach them as an error rather than a 200 carrying the same stale
    url they were trying to replace. The unsafe forms are called on purpose.

    Also deliberately SYNCHRONOUS. A remote browser render takes seconds, which is
    too long to block a publish and exactly right for a request whose entire
    purpose is the answer.

    Routes itself the same way the automatic path does: a site with a url is
    photographed live, a draft is photographed from its own markup. Tenant-scoped
    via ``_load``, so another workspace's site is a 404, never a render.

    Runs the same readiness gate the deploy path does, on a SHORT budget, and reports
    a page that is not serving as its own ``sites.preview_not_serving`` — see the
    comment at the branch. This is also the recovery path for a capture the deploy
    path DECLINED: when an edge takes longer than the post-deploy budget to come up,
    the card is deliberately left without a picture, and this is what fills it in.
    """
    from pocketpaw_ee.sites.screenshot import (
        _READY_DELAYS_MANUAL,
        take_draft_screenshot,
        take_site_screenshot,
        wait_until_serving,
    )

    site = await _load(workspace_id, site_id)

    url = (getattr(site, "url", "") or "").strip()
    if url:
        # The readiness gate, run HERE as well as inside the capture, purely so this
        # path can name what went wrong. ``take_site_screenshot`` reports every
        # decline the same way — with "" — and the two declines need opposite
        # advice: a site with nothing renderable yet should be published, while a
        # site that IS published and merely still coming up should just be retried.
        # On a short budget, because a person is watching a spinner: the deploy
        # path's minute is right for a background task and wrong for a request.
        if not await wait_until_serving(url, delays=_READY_DELAYS_MANUAL):
            raise ValidationError(
                "sites.preview_not_serving",
                "The site isn't answering yet. A deploy can take a moment to go "
                "live at the edge — try the refresh again shortly.",
            )
        # A single confirming probe immediately before the paid render, so the gate
        # has no bypass path: every call into ``take_site_screenshot`` is gated.
        image_url = await take_site_screenshot(site, ready_delays=())
    else:
        image_url = await take_draft_screenshot(site)

    if not image_url:
        # The capture declined rather than failed: a draft with nothing renderable
        # yet, or a build this deployment has not opted into (see
        # ``draft_markup.build_allowed``). Returning 200 with the previous url would
        # report success for a refresh that did not happen, and a 500 would blame
        # the server for a site that simply has no page to photograph.
        raise ValidationError(
            "sites.preview_unavailable",
            "There's nothing to photograph yet — publish the site, or open its "
            "preview once so there's a page to capture.",
        )

    return SitePreviewRefreshResponse(site_id=site_id, preview_image_url=image_url)


# How long a Site may sit in ``provision_status="provisioning"`` before a new
# publish stops treating it as in-flight. A real dynamic provision (D1 create,
# migration, build, deploy) has been measured at ~5 minutes, so this is generous;
# it exists purely so a job that was never consumed or died mid-flight cannot
# brick the pocket permanently.
_PROVISION_STALE_AFTER = timedelta(minutes=30)


def _provisioning_is_stale(doc: Any) -> bool:
    """True when a ``provisioning`` Site's last update is older than the window.

    Missing/unreadable timestamps read as STALE: a doc we cannot date is far more
    likely to be a leftover than a live job, and the failure modes are asymmetric
    — a redundant enqueue costs one idempotent job, while a stuck guard costs the
    pocket every future publish.
    """
    stamp = getattr(doc, "provision_started_at", None)
    if stamp is None:
        return True
    try:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return datetime.now(UTC) - stamp > _PROVISION_STALE_AFTER
    except (AttributeError, TypeError, ValueError):
        return True


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
    #
    # BOUNDED, because an unbounded guard is a trap. If the job is never consumed
    # (no worker running) or dies without writing a terminal status, the doc stays
    # "provisioning" forever and EVERY later publish of that pocket is silently a
    # no-op — the pocket becomes permanently unpublishable with no error anywhere
    # to see. Two sites on the rig were bricked exactly this way (2026-07-31).
    # After the stale window we treat the previous attempt as lost and fall through
    # to enqueue a fresh one: a genuinely in-flight job is far shorter than this,
    # and the job is idempotent (the D1 id is persisted before the build and reused
    # on retry).
    if doc is not None and doc.provision_status == "provisioning":
        if _provisioning_is_stale(doc):
            logger.warning(
                "sites: site %s has been provisioning since %s — treating that job "
                "as lost and re-enqueueing, rather than no-op'ing every publish "
                "of this pocket forever",
                doc.id,
                getattr(doc, "provision_started_at", None),
            )
        else:
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
            provision_started_at=datetime.now(UTC),
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
        doc.provision_started_at = datetime.now(UTC)
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


# ---------------------------------------------------------------------------
# SL-3 — the async static publish.
# ---------------------------------------------------------------------------


def build_runs_async(engine: str | None) -> bool:
    """Does publishing this engine ENQUEUE its build instead of running it inline?

    True for exactly the engines whose ephemeral-lane artifact can actually be
    DEPLOYED, which today is react alone. This is a narrow answer to a broad-sounding
    question, and the narrowness is the whole content of the predicate:

    * ``html`` runs no build at all (``needs_node_build`` is False), so there is
      nothing to enqueue. Flipping it would add a queue wait to the one engine that
      never needed one.
    * ``ripple`` and DYNAMIC ``svelte`` build on adapter-cloudflare, whose output's
      pages are rendered by a ``_worker.js`` whose imports sit OUTSIDE the tarred
      directory. The artifact therefore cannot serve — which is not a guess: it is why
      ``truth_lane`` refuses to even PREVIEW one (``REASON_WORKER_RENDERED``). Queueing
      those builds would replace a working publish with one nothing can deploy.
    * STATIC ``svelte`` (adapter-static) IS self-sufficient, and is still excluded,
      because which adapter ran is a property of the built SITE and is not knowable at
      enqueue time — only after the build. A gate has to decide before it spends the
      queue, so svelte stays inline until the artifact question is settled for the
      whole track.
    * ``react`` emits a prerendered, assets-only ``dist`` with no server entry, so the
      tar is the whole deployable site.

    Widen this ONLY together with the artifact: the moment an adapter-cloudflare
    artifact can serve, ripple and svelte belong here too, and this predicate is the
    one place that changes.
    """
    return normalize_engine(engine) == "react"


async def _enqueue_static_build(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    site_id: str,
    signed_key: str,
    site_name: str,
    ripple_spec: dict[str, Any] | None,
    theme: dict[str, Any],
    engine: str,
    source: dict[str, str] | None,
    assets: dict[str, str] | None,
    pattern: str | None,
    builder_origin: str | None,
    keeps_client_bundle: bool,
) -> _SiteDoc:
    """Enqueue a static site's build and return immediately (SL-3).

    The async half of ``_deploy_site_doc``. It ensures the ONE canonical Site doc for
    ``(workspace, pocket_id)`` exists, stamps the build lifecycle onto it, and hands the
    build to the ephemeral lane. The worker calls back into
    :func:`deploy_prebuilt_site` when the artifact is ready, so the deploy, the upsert
    and the post-deploy scheduling all still happen exactly once, in one place.

    A RE-PUBLISH DOES NOT TAKE THE LIVE SITE DOWN, and that is the important difference
    from ``_provision_dynamic_site``, which sets ``deployed=False`` / ``url=""`` while
    its job runs. Here the previous deploy is still serving the moment a rebuild is
    queued, so clearing those fields would report a working site as not-live for the
    length of a build. ``build_status`` carries the in-flight state instead — the wire
    contract the frontend already codes to ("a site can be live and simultaneously
    mid-rebuild").

    Returns the doc carrying ``build_status`` / ``build_started_at`` / ``build_job_id``,
    which ``_to_response`` surfaces so the client has something to poll. On a
    single-flight no-op (a build is genuinely already in flight) the doc comes back
    unchanged, still carrying the in-flight build's own job id — the caller is watching
    the right build, just not a new one.
    """
    # Lazy import: keeps the sites service free of an eager arq/Redis dependency at
    # module load, and breaks the cycle (``build_job`` imports this module).
    from pocketpaw_ee.sites import build_job
    from pocketpaw_ee.sites.generator_client import build_generator_input

    oid = ObjectId(site_id)
    doc = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        doc = _SiteDoc(
            id=oid,
            workspace=workspace_id,
            pocket_id=pocket_id,
            owner=user_id,
            name=site_name,
            script_name=site_id,
            # A first publish has nothing serving yet, so this is honest rather than
            # pessimistic: the worker flips both when the deploy succeeds.
            deployed=False,
            url="",
            signed_key=signed_key,
            builder_origin=builder_origin or "",
            allowed_origins=_default_allowed_origins(),
            event_mapping=_DEFAULT_EVENT_MAPPING,
        )
        await doc.insert()
    else:
        # Refresh only the identity fields a re-publish legitimately changes. NOT
        # ``deployed`` / ``url`` (the prior deploy is still live), NOT
        # ``allowed_origins`` / ``domains`` / ``signed_key`` (a connected domain and the
        # capture key must survive a rebuild).
        doc.pocket_id = pocket_id
        doc.owner = user_id
        doc.name = site_name
        doc.script_name = site_id
        await doc.save()

    generator_input = build_generator_input(
        engine=engine,
        theme=theme,
        site_id=site_id,
        title=site_name,
        capture_api_base=_capture_base(),
        capture_signed_key=signed_key,
        ripple_spec=ripple_spec,
        source=source,
        assets=assets,
        builder_origin=builder_origin,
        keeps_client_bundle=keeps_client_bundle,
    )
    # The inputs the worker hands back to ``deploy_prebuilt_site`` so the deploy tail
    # runs with exactly what this publish captured, not with whatever the pocket's
    # draft has become by the time the build finishes.
    deploy_inputs = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "pocket_id": pocket_id,
        "site_id": site_id,
        "signed_key": signed_key,
        "site_name": site_name,
        "engine": engine,
        "pattern": pattern,
        "builder_origin": builder_origin,
    }

    # An enqueue failure PROPAGATES. The helper rolls the row to a terminal status
    # first, so the site stays republishable, and the raise becomes the 5xx the user
    # sees. Returning success for a build that was never queued is the one outcome
    # worse than a failed publish: the row would read in-progress forever and the UI
    # would poll a job that does not exist.
    job_id = await build_job.enqueue_site_build(
        doc,
        engine=engine,
        generator_input=generator_input,
        deploy_inputs=deploy_inputs,
    )
    logger.info(
        "sites: queued the build for site %s (pocket %s, engine %s) as job %s",
        site_id,
        pocket_id,
        engine,
        job_id,
    )
    # Re-read so the response carries what the enqueue just wrote (the helper stamps
    # through a targeted ``set``, so the in-memory doc this function holds is stale).
    fresh = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
    return fresh if fresh is not None else doc


async def deploy_prebuilt_site(
    *,
    project_dir: str,
    deploy_inputs: dict[str, Any],
    _local_deploy: Callable[[str, str], str] | None = None,
    _workers_deploy: Callable[..., Any] | None = None,
) -> _SiteDoc:
    """Finish a publish whose build already happened elsewhere (SL-3).

    The seam the build worker calls once it has materialised the artifact. It runs
    ``_deploy_site_doc``'s tail — concierge embed → deploy → canonical upsert →
    knowledge sync + screenshot — against the prebuilt tree, so there is exactly ONE
    implementation of "what happens after a site is built" and the async path cannot
    drift from the inline one.

    ``deploy_inputs`` is the dict ``_enqueue_static_build`` put in the job payload, so
    the deploy uses what the PUBLISH captured. The worker passes no injection seams, so
    ``PAW_CF_DEPLOY_MODE`` selects the target exactly as it does inline (local mode still
    serves from ``local_server.deploy_local``); the two underscore-prefixed parameters
    exist only so a test can drive this seam without a real deploy target, mirroring
    ``publish``'s own ``_local_deploy`` / ``_workers_deploy``.
    """
    return await _deploy_site_doc(
        ripple_spec=None,
        theme={},
        prebuilt_project_dir=project_dir,
        local_deploy=_local_deploy,
        workers_deploy=_workers_deploy,
        **deploy_inputs,
    )


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


def build_wire_state(doc: _SiteDoc | None) -> dict[str, Any]:
    """The build fields an AGENT-facing tool reports, derived in ONE place (RX-4).

    ``_to_response`` already surfaces ``build_status`` / ``build_reason`` /
    ``build_job_id`` to the frontend, which polls them alongside ``url`` and knows
    that a site can be live and simultaneously mid-rebuild. The chat agent has no
    such knowledge and cannot poll, so it needs the same three fields PLUS the
    conclusion drawn from them. This function is that conclusion, and it lives here
    rather than in the two calling handlers because the publish response and the
    build-status tool disagreeing about whether a site is live is worse than either
    being wrong on its own.

    The three raw fields pass through VERBATIM, matching ``_to_response``: a
    ``build_status`` this deploy predates must never be normalised against a known
    set, because mapping it to "none" would tell a caller nothing is building about
    a build that is running.

    ``build_in_progress`` reads an unknown status as IN PROGRESS. That is the WIRE
    direction and it is deliberately the OPPOSITE of ``build_state.should_enqueue``,
    which treats an unknown status as terminal — both are right on their own axis
    (see ``build_state``'s header: a redundant build costs one sandbox, while a
    spurious "your site is live" costs the user's trust). Derived from
    ``TERMINAL_STATUSES`` rather than from ``IN_FLIGHT_STATUSES`` so a state added
    to the machine defaults to in-progress here without anyone remembering to
    update this function.

    ``is_live`` is the only field an agent should gate "show the user this url" on.
    It requires a real url AND a successful deploy AND no build in flight, because
    each of the three is individually insufficient:

      * a FIRST async publish creates the Site doc with ``url=""`` and
        ``deployed=False`` (``_enqueue_static_build`` — honest, nothing is serving
        yet), so ``url`` alone is an empty string the agent would hand over;
      * a RE-publish deliberately KEEPS the previous deploy's ``url`` and
        ``deployed=True`` so a rebuild never reports a working site as down, so
        those two alone say "live" while serving the pre-change page;
      * ``build_status`` alone cannot tell a never-built pocket ("none") from a
        finished one.

    Pure and I/O-free, so it is directly unit-testable, and it takes ``None`` for a
    pocket with no Site doc at all (never published) rather than making every caller
    write the same empty shape.
    """
    from pocketpaw_ee.sites.build_state import TERMINAL_STATUSES

    if doc is None:
        return {
            "url": "",
            "deployed": False,
            "build_status": "none",
            "build_reason": None,
            "build_job_id": None,
            "build_in_progress": False,
            "is_live": False,
        }
    status = getattr(doc, "build_status", "none")
    in_progress = status != "none" and status not in TERMINAL_STATUSES
    url = doc.url or ""
    return {
        "url": url,
        "deployed": bool(doc.deployed),
        "build_status": status,
        "build_reason": getattr(doc, "build_reason", None),
        "build_job_id": getattr(doc, "build_job_id", None),
        "build_in_progress": in_progress,
        "is_live": bool(url) and bool(doc.deployed) and not in_progress,
    }


async def site_build_status(*, workspace_id: str, pocket_id: str) -> dict[str, Any]:
    """Read a pocket's current build + live state. READ-ONLY (RX-4).

    The answer to "is it up yet?" on a turn AFTER the publish. Without it the queued
    state a react publish returns is a dead end: the agent learns a build was
    enqueued and then has no way to ever discover it finished, because
    ``build_runs_async("react")`` means the publish call returned before the build
    even started.

    Resolves the ONE canonical Site doc for the pocket through
    ``canonical_site_for_pocket``, which is tenant-scoped on ``workspace_id`` and
    dedupe-aware — the same resolution ``pocket_status`` uses, so the two cannot
    report different sites for one pocket.

    A pocket with no Site doc returns ``published=False`` rather than raising. From
    the agent's side "this was never published" is the useful answer and is correct
    whether the pocket has no site or does not exist; the read cannot leak across
    tenants either way, because the query is filtered on ``workspace``.

    No plan gate: nothing is mutated, and a workspace that has lost the Sites
    feature reading its own build state changes nothing it could not already see in
    the /sites UI. The tenancy filter IS the access check here.
    """
    doc = await canonical_site_for_pocket(workspace_id, pocket_id)
    state = build_wire_state(doc)
    return {
        "pocket_id": pocket_id,
        "site_id": str(doc.id) if doc is not None else None,
        "name": doc.name if doc is not None else "",
        "published": doc is not None,
        **state,
    }


async def canonical_site_for_pocket(workspace_id: str, pocket_id: str) -> _SiteDoc | None:
    """Public: the ONE canonical Site doc for (workspace, pocket_id), or None.

    Thin, tenant-scoped wrapper over ``_canonical_site_doc`` so callers outside
    this module (the paw-bar concierge auto-provisioner) resolve a pocket to its
    live Site through the SAME dedupe-aware logic the rest of the sites stack uses,
    without reaching into a private helper.
    """
    return await _canonical_site_doc(workspace_id, pocket_id)


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
) -> tuple[str, str]:
    """Deploy a PROVISIONED dynamic site to whichever target ``_deploy_mode()`` names,
    and return ``(public_url, resolved_target)``. The one seam the provision job
    deploys through.

    The RESOLVED target is returned, not left for the caller to re-derive, because this
    function is the only place that knows it: ``local`` degrades to ``workers`` below,
    so re-reading ``PAW_CF_DEPLOY_MODE`` afterwards gives an answer that disagrees with
    what was actually deployed. The custom-domain lane keys a site's Worker route off
    that answer, so a disagreement there writes the route against the wrong target — or
    silently writes none.

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

        url = await workers_deploy_mod.deploy_workers(
            site_id, project_dir, d1_database_id=d1_database_id
        )
        return url, "workers"

    cf = cloudflare or _cf_client()
    await cf.put_worker(
        script_name=site_id,
        bundle=bundle,
        bindings=provision_d1_bindings(d1_database_id),
    )
    return provision_site_url(site_id), "wfp"


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


async def finalize_provisioned_site(site: _SiteDoc, *, url: str, deploy_target: str = "") -> None:
    """Mark a Site doc fully provisioned after migrate + deploy succeed (DP0-3):
    ``provision_status="provisioned"``, ``deployed=True``, stamp the live URL +
    ``deployed_at``. The last write of the durable job's happy path.

    ``deploy_target`` is the target ``provision_deploy`` RESOLVED — which is not the
    configured mode, because it degrades ``local`` to ``workers`` for a dynamic site.
    Passed in rather than re-derived here: re-reading the env would ask the question a
    second time and could get a different answer, which is the whole class of bug this
    field exists to end. Defaulted to "" so an older caller writes nothing rather than
    something wrong."""
    site.provision_status = "provisioned"
    site.deployed = True
    site.deployed_at = datetime.now(UTC)
    site.url = url
    if deploy_target:
        site.deploy_target = deploy_target
    await site.save()
    # A dynamic site stands up through this job rather than through
    # ``_deploy_site_doc``, so it needs its own knowledge sync or its concierge
    # would be the only one left knowing nothing about the business.
    _schedule_site_knowledge_sync(site)
    # SC-1: and its own screenshot, for the same reason — this is the moment a
    # dynamic site becomes reachable, so it is the moment there is a page to
    # photograph. The url was stamped a few lines above.
    _schedule_site_screenshot(site)


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


# ---------------------------------------------------------------------------
# SL-2 — the ephemeral-build lane's Site-doc seams.
#
# ``sites/build_job.py`` owns the build (scaffold → sandbox → classify → settle); this
# module owns the Site document, the same split DP0-3's seams above make for the provision
# job. The build lane never imports the Beanie doc.
#
# EVERY WRITE HERE IS A TARGETED ``set``, NOT A ``save()``, and that is the one thing not
# to change. A build runs for minutes alongside a publish that may be writing ``url`` /
# ``deployed`` / ``name`` on the same row; a full ``save()`` from a doc loaded before that
# publish would silently roll those fields back to their pre-publish values. The build
# lane has no business writing anything but its own four fields. (``preview_image_url``
# is written the same way, for the same reason.)
# ---------------------------------------------------------------------------


async def load_build_site(workspace_id: str, site_id: str) -> _SiteDoc | None:
    """The Site row a build job operates on, or ``None``.

    Scoped to ``workspace_id`` like ``_load``: the job is handed an id, and a read that
    ignored the workspace would let a bad payload move another tenant's row. Returns None
    rather than raising for a deleted site or a malformed id — the job then no-ops, which
    is the only sane response to "there is nothing to record on".
    """
    try:
        oid = ObjectId(site_id)
    except (InvalidId, TypeError):
        return None
    return await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})


async def claim_build_queued(
    site: _SiteDoc,
    *,
    job_id: str,
    timeout_seconds: int,
    now: datetime | None = None,
) -> bool:
    """CLAIM the build slot for ``site`` and stamp it ``queued``. False means lost.

    One write, all four fields, CONDITIONAL on the row still being claimable.
    ``build_started_at`` is what bounds the single-flight guard — without it the row reads
    as stale immediately and a second publish opens a second sandbox. ``build_job_id`` is
    persisted (not a transient PrivateAttr like DP0-4's) because a queued build is exactly
    when a user reloads. ``build_reason`` is cleared so a new attempt never shows the
    previous attempt's rung.

    Rewritten 2026-08-11 from ``mark_build_queued``, which wrote unconditionally. Reading
    ``should_enqueue`` and then stamping is two steps with an await between them, and every
    publish arriving inside that window read the same pre-stamp row, passed the gate
    correctly, and opened its own sandbox: 8 concurrent publishes of one site produced 8
    sandboxes. The precondition moves the decision into the write, so the DATABASE picks
    one winner. Unlike the other three build seams this is the one write that must NOT be
    a blind ``set``.

    THE PRECONDITION IS ``build_state.claim_precondition`` AND NOT A LOCAL REPHRASING.
    It has to permit exactly what ``should_enqueue`` permits, staleness included: a
    precondition that refused a stale in-flight row would recreate the one-way door and
    leave the site permanently unpublishable. The same ``now`` is used for the window test
    and the stamp, so the row is never tested against a window it was not written with.

    Returns True when this caller owns the build and False when another publish claimed it
    first. False is not an error — it is the same "a build is already in flight" answer the
    caller returned before, and it is the losing publish's cue to write nothing at all.
    """
    stamp = now or datetime.now(UTC)
    values: dict[str, Any] = {
        "build_status": "queued",
        "build_started_at": stamp,
        "build_job_id": job_id,
        "build_reason": None,
    }
    collection = type(site).get_pymongo_collection()
    won = await collection.find_one_and_update(
        {"_id": site.id, **claim_precondition(timeout_seconds, now=stamp)},
        {"$set": values},
    )
    if won is None:
        return False
    # The write went to the DB, so the in-memory doc is now behind it. Every later step
    # of the enqueue reads this object (the log line, the rollback), and a caller that
    # won the claim but still saw a stale local row would report the pre-claim status.
    for field, value in values.items():
        setattr(site, field, value)
    return True


async def mark_build_running(site: _SiteDoc) -> None:
    """Flip a queued build to ``building`` and RE-STAMP the clock.

    Re-stamping is deliberate: ``build_started_at`` means "when the current attempt
    started", and the attempt's real clock begins when a worker picks the job up. Leaving
    the enqueue's stamp would spend the site's staleness window on queue wait, so a build
    that waited behind the cap could be declared stale while it was still running — and
    re-enqueued on top of itself, which is the expensive direction.
    """
    await site.set({"build_status": "building", "build_started_at": datetime.now(UTC)})


async def record_build_outcome(site: _SiteDoc, *, status: str, reason: str) -> None:
    """Record a finished attempt's terminal status and the rung that produced it.

    ``reason`` is a fixed ``"<rung>:<cause>"`` identifier from ``build_job``, NEVER build
    stderr — see ``Site.build_reason`` and ``build_job``'s header. This seam does not
    enforce that (a string is a string); the vocabulary is owned and tested where it is
    produced, and the mutation plan is what keeps stderr out of it.

    ``build_job_id`` is left in place: a client that polled with that handle must still
    find the row it was watching once the build ends.
    """
    await site.set({"build_status": status, "build_reason": reason})


def _normalize_hostname(hostname: str) -> str:
    """One spelling for a hostname, applied on every path that names one.

    DNS is case-insensitive and the trailing FQDN dot is optional, so ``Example.com``,
    ``example.com.`` and ``example.com`` are the same name — but Python string equality
    is not, and this module finds a stored domain with ``==``. Cloudflare echoes back a
    lowercased hostname, which is what gets stored, so without this
    ``DELETE .../domains/Example.com`` 404s a domain that is plainly connected, and
    adding ``Example.com`` after ``example.com`` slips the dedupe guard into a
    Cloudflare 1406 instead of the friendly early return.

    The DTO validator already strips whitespace and the trailing dot on the ADD path;
    this is the shared spelling every path uses, so the two cannot drift."""
    return (hostname or "").strip().rstrip(".").lower()


def _route_pattern(hostname: str) -> str:
    """The Worker-route pattern for a custom hostname: every path on that host.

    Cloudflare route patterns are ``<host>/<path>``; a bare host matches only the
    root, which would serve the home page and 404 everything else — a failure that
    looks like a broken site rather than a broken route."""
    return f"{hostname}/*"


def _route_target(site: Any) -> str:
    """The Worker name a custom domain's route should point at, or ``""`` when nothing
    route-addressable was deployed for this site.

    A route is meaningful only where the site was deployed as its OWN addressable
    Worker (``paw-site-<id>``, via ``workers_deploy``). ``wfp`` uploads into a dispatch
    namespace, where the script is not route-addressable at all and the namespace's own
    dispatch Worker routes; ``local`` serves from localhost. Those keep the prior
    hostname-only behaviour rather than writing a route naming a script Cloudflare
    cannot find.

    **This asks the SITE what was deployed, never the environment what is configured.**
    Two earlier versions of this function read ``PAW_CF_DEPLOY_MODE`` and both were
    wrong, in opposite directions, because the mode is read at request time while the
    Worker was made at deploy time:

    * ``provision_deploy`` degrades ``local`` to ``workers`` for a dynamic site, so a
      dynamic site on a local-mode box has a real Worker the mode denies.
    * ``provision_status`` (the second attempt) records that *a* deploy finished, not
      which target it used, and a REPUBLISH resets it to ``"provisioning"`` while last
      deploy's Worker is still live and serving. That window is the ordinary lifecycle
      of any site that has been up for a while — and worse than the first-provision
      case, because afterwards the status returns to ``"provisioned"`` and the row looks
      healthy while permanently missing its route.
    * Nothing in this codebase ever deletes a Worker, so a site published under
      ``workers`` keeps its ``paw-site-<id>`` Worker after the env moves to ``wfp`` —
      and a wfp-provisioned site never had one, whatever the env says now.

    ``deploy_target`` is stamped after a deploy RETURNS, so it is the only value that
    describes what actually exists. Same "ask the artifact, not the intent" move
    ``workers_deploy`` made for engines in SL-1, one level up.

    **Rows that predate the field.** ``deploy_target`` is new, so every site deployed
    before it shipped carries ``""`` — which is indistinguishable, by the field alone,
    from "never deployed". Reading it strictly would mean no already-live site could
    connect a domain until it was republished, and the failure would be the silent kind:
    a hostname created, no route written, the fallback origin served. So a row that is
    ``deployed`` but unstamped falls back to the deploy MODE, which is exactly the answer
    this code used before the field existed — not a regression, and self-healing, because
    the next publish stamps the real value and this branch stops being reached.

    The fallback is deliberately NOT extended to unstamped rows that are not
    ``deployed``: there, "" really does mean no Worker.

    The name comes from ``workers_deploy.worker_name`` rather than being rebuilt here:
    a route naming a script that does not exist is rejected, so the deploy's answer and
    this one have to be the same function."""
    target = site.deploy_target
    if not target and site.deployed:
        # Migration bridge for pre-field rows. Mirrors provision_deploy's local->workers
        # degradation for a provisioned (dynamic) site, the same way the pre-field code
        # had to.
        target = _deploy_mode() or ""
        if target == "local" and site.provision_status == "provisioned":
            target = "workers"
    if target != "workers":
        return ""
    from pocketpaw_ee.sites.workers_deploy import worker_name

    return worker_name(str(site.id))


async def add_domain(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> DomainStatusResponse:
    """Register a custom hostname with Cloudflare and route it to this site.

    Two calls, and the second is the one that was missing. ``create_custom_hostname``
    only gets Cloudflare to ACCEPT traffic for the domain; a Worker route scoped to
    ``<hostname>/*`` is what decides that THIS site answers it. Without the route the
    domain validates, goes green in the panel, and serves an error page from the
    fallback origin — the worst kind of broken, because every signal says it worked.

    Returns the ONE CNAME the client pastes at their registrar.

    **A site must be published first.** A route can only name a Worker that exists, and
    an unpublished site has none. Refusing here costs the user one ordering hint;
    allowing it produces a domain that is live-looking and dead, with nothing in the UI
    able to explain the difference.

    **A failed route is rolled back.** If the route call fails after the hostname was
    created, the hostname is deleted again before the error propagates. Cloudflare
    rejects a duplicate hostname (1406), so leaving the half-made pair behind would
    make the user's obvious next move — press Add again — fail with an error about a
    conflict they cannot see or clear.

    **Re-adding a connected domain REPAIRS a missing route.** Every domain connected
    before this lane shipped has no route and is silently serving the fallback origin,
    and Cloudflare reports those hostnames ``active``, so the panel shows them green.
    Without this, the one self-service action available — press Add again — became a
    no-op that returned the stored row, so nothing a user could do would fix them. The
    repair is the same call the first add makes, so it costs nothing to reach and it
    also recovers a route lost to a partial failure.
    """
    # BC-10: a higher site-plan tier resells Cloudflare paid features (WAF /
    # edge-cache / strict TLS). Lazy import mirrors publish_pocket's site_plans
    # use — keeps the billing catalog off the module-import path.
    from pocketpaw_ee.cloud.billing import site_plans

    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)

    # Already connected → return what we have, without touching Cloudflare. Adding
    # the same hostname twice used to append a SECOND row (only allowed_origins was
    # de-duped), which under routing means two routes for one pattern and a teardown
    # that removes half of it. Cloudflare would reject the duplicate hostname anyway
    # (1406); answering from the stored row turns "you pressed Add twice" into the
    # same CNAME instruction rather than an error about a conflict with itself.
    hostname = _normalize_hostname(hostname)
    existing = next((d for d in site.domains if _normalize_hostname(d.hostname) == hostname), None)
    if existing is not None:
        # ...but repair a MISSING route first. Domains connected before this lane
        # shipped all have ``cf_route_id == ""`` and no route, so they serve the
        # fallback origin while Cloudflare reports the hostname active and the panel
        # shows them green. Pressing Add again is the only self-service action there is;
        # without this it returned the stored row and changed nothing. Also recovers a
        # route lost to a partial failure.
        script = _route_target(site)
        if script and not existing.cf_route_id:
            existing.cf_route_id = await cf.create_worker_route(
                pattern=_route_pattern(existing.hostname), script=script
            )
            await site.set({"domains": [d.model_dump() for d in site.domains]})
        return DomainStatusResponse(
            hostname=existing.hostname,
            cname_target=existing.cname_target,
            status=existing.status,
        )

    script = _route_target(site)
    if script and not site.deployed:
        raise ValidationError(
            "sites.domain_needs_publish",
            "Publish the site before connecting a domain — a custom domain has to "
            "point at a published site, and this one hasn't been published yet.",
        )

    # Resolve the site's tier → its cloudflare_features and provision them on the
    # custom hostname. A base-tier (or unknown) site resolves to an empty set, so
    # create_custom_hostname stays on the basic path.
    plan = site_plans.get_site_plan(site.plan_tier)
    features = set(plan.cloudflare_features) if plan else set()
    ch = await cf.create_custom_hostname(hostname, features=features)

    route_id = ""
    if script:
        try:
            route_id = await cf.create_worker_route(
                pattern=_route_pattern(ch.hostname), script=script
            )
        except Exception:
            # Compensate, then re-raise the ORIGINAL failure: the rollback is
            # housekeeping, and reporting its outcome instead would replace the real
            # reason the domain could not be added with a second, less useful one.
            try:
                await cf.delete_custom_hostname(ch.id)
            except Exception:  # noqa: BLE001 — best effort; the original error wins
                logger.exception(
                    "sites: could not roll back custom hostname %s after a failed "
                    "route create — it may need removing by hand",
                    ch.hostname,
                )
            raise

    site.domains.append(
        _SiteDomainDoc(
            hostname=ch.hostname,
            cf_hostname_id=ch.id,
            cname_target=ch.cname_target,
            status=ch.status.value,
            cf_route_id=route_id,
        )
    )
    # Authorize the site's own origin for capture: the deployed form posts from
    # this host, and origin_allowed host-matches it against allowed_origins. Done
    # here so connecting a domain needs no separate "allow this origin" step.
    if ch.hostname not in site.allowed_origins:
        site.allowed_origins.append(ch.hostname)
    # Targeted ``set``, not ``save()`` — this module's own header (the build-lane note)
    # warns why: a build runs for MINUTES beside a publish writing ``url`` / ``deployed``
    # / ``build_status`` on the same row, and a full save from a doc loaded before those
    # writes rolls them back. Land after the terminal build write and ``build_status``
    # reverts to in-flight permanently, at which point ``build_state.should_enqueue``
    # refuses to republish that site ever again. Only these two fields changed here.
    await site.set(
        {
            "domains": [d.model_dump() for d in site.domains],
            "allowed_origins": list(site.allowed_origins),
        }
    )
    return DomainStatusResponse(
        hostname=ch.hostname, cname_target=ch.cname_target, status=ch.status.value
    )


async def remove_domain(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> None:
    """Disconnect a custom domain: drop its route, its hostname, and its origin.

    There was no teardown path at all before this, and its absence is not cosmetic.
    A hostname left on the zone counts against the account's quota forever, keeps
    pointing at a Worker that may no longer exist, and — because Cloudflare rejects
    duplicates — permanently blocks anyone from connecting that domain to a different
    site.

    Ordering is deliberate: the ROUTE goes first, so the domain stops being served
    before it stops being recognised. The reverse order leaves a window where
    Cloudflare no longer knows the hostname while a route still claims it.

    Both deletes treat a Cloudflare 404 as success (see ``cloudflare_client``): the
    goal is a state, not an event, and a teardown that cannot finish leaves exactly the
    orphan it was called to remove. The local row is dropped even if Cloudflare has
    already forgotten both — otherwise a partially-torn-down domain would be
    un-removable from the UI forever.
    """
    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)
    hostname = _normalize_hostname(hostname)
    dom = next((d for d in site.domains if _normalize_hostname(d.hostname) == hostname), None)
    if dom is None:
        raise NotFound("domain", hostname)

    if dom.cf_route_id:
        await cf.delete_worker_route(dom.cf_route_id)
    if dom.cf_hostname_id:
        await cf.delete_custom_hostname(dom.cf_hostname_id)

    site.domains = [d for d in site.domains if _normalize_hostname(d.hostname) != hostname]
    # The origin was authorized by add_domain purely so this domain's forms could
    # post; with the domain gone it is a standing grant to a host we no longer serve.
    site.allowed_origins = [o for o in site.allowed_origins if _normalize_hostname(o) != hostname]
    # Targeted ``set`` for the same reason ``add_domain`` uses one — see there.
    await site.set(
        {
            "domains": [d.model_dump() for d in site.domains],
            "allowed_origins": list(site.allowed_origins),
        }
    )
    # no-event: no SiteDomain event type exists — add_domain does not emit one either,
    # and inventing a half of the pair here would leave connects silent and
    # disconnects loud. Both belong in the reconciler work the design defers.


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
    hostname = _normalize_hostname(hostname)
    dom = next((d for d in site.domains if _normalize_hostname(d.hostname) == hostname), None)
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


# How many manual receipts one site's client record retains, newest first. A site
# billed monthly for a decade is 120 rows, so this is generous enough that no real
# owner meets it — it exists to bound the document, not to ration the feature.
_INVOICE_KEEP = 500


def _client_response(site: _SiteDoc) -> SiteClientResponse:
    """Project a Site doc onto the client-record wire shape. ``issued_at`` is
    serialized here rather than by the DTO so the wire always carries a plain ISO
    string, matching every other timestamp the sites surface returns."""
    return SiteClientResponse(
        site_id=str(site.id),
        name=site.client_name,
        contact=site.client_contact,
        notes=site.client_notes,
        invoices=[
            SiteInvoiceOut(
                id=inv.id,
                issued_at=inv.issued_at.isoformat(),
                amount_cents=inv.amount_cents,
                currency=inv.currency,
                paid=inv.paid,
                note=inv.note,
            )
            for inv in site.client_invoices
        ],
    )


async def get_site_client(*, workspace_id: str, site_id: str) -> SiteClientResponse:
    """Return the owner's client record for a site (name / contact / notes and the
    manual receipts recorded against it). Tenant-scoped via ``_load``, so a missing
    or cross-tenant site is a 404.

    A site that has never had a client recorded returns a BLANK record, not a 404:
    "no client yet" is the ordinary starting state of every site, and the Settings
    form renders the same fields either way. Reserving 404 for "no such site" keeps
    the two genuinely different failures distinguishable at the edge.
    """
    site = await _load(workspace_id, site_id)
    return _client_response(site)


async def update_site_client(
    *, workspace_id: str, site_id: str, body: SiteClientUpdate
) -> SiteClientResponse:
    """Patch the owner's client record. THREE-WAY: a field absent from the body is
    left alone, while an explicitly-sent empty string clears it — which is how the
    form deletes a value without a separate endpoint. ``model_fields_set`` is what
    tells the two apart, so this must read the un-dumped model.

    Writes through a targeted ``set()`` rather than ``save()`` on purpose. This is a
    human-paced edit against a document a BUILD also writes to: an owner typing
    notes while a publish settles would, under ``save()``, push their whole stale
    snapshot back and silently roll ``build_status`` backwards. A field-scoped
    update cannot.
    """
    body = SiteClientUpdate.model_validate(body)
    site = await _load(workspace_id, site_id)

    updates: dict[str, Any] = {}
    if "name" in body.model_fields_set:
        updates["client_name"] = (body.name or "").strip()
    if "contact" in body.model_fields_set:
        updates["client_contact"] = (body.contact or "").strip()
    if "notes" in body.model_fields_set:
        updates["client_notes"] = body.notes or ""

    # An empty PATCH is a no-op read, not an error — a form that autosaves on blur
    # will send one, and failing it would surface as a spurious error toast.
    if updates:
        await site.set(updates)

    # no-event: the client record is the owner's own bookkeeping. Nothing
    # downstream (search index, soul memory, ripple invalidation, the deploy lane)
    # reads it, and it never reaches a generated page.
    return _client_response(site)


async def record_site_invoice(
    *, workspace_id: str, site_id: str, body: SiteInvoiceCreate
) -> SiteClientResponse:
    """Append one manual receipt to the site's client record and return the whole
    updated record (so the caller re-renders from one authoritative response rather
    than splicing the new row in locally).

    Nothing here charges anyone — it is the owner writing down that their client
    paid. The list is capped at ``_INVOICE_KEEP`` newest-first entries so a site
    that is billed monthly for years cannot grow a document without bound; the cap
    drops the OLDEST, which is the only end that can be dropped without losing the
    balance the owner is actually looking at.
    """
    body = SiteInvoiceCreate.model_validate(body)
    site = await _load(workspace_id, site_id)

    entry = _SiteInvoiceDoc(
        id=f"inv_{secrets.token_hex(8)}",
        issued_at=datetime.now(UTC),
        amount_cents=body.amount_cents,
        currency=body.currency,
        paid=body.paid,
        note=body.note.strip(),
    )
    kept = [entry, *site.client_invoices][:_INVOICE_KEEP]
    # Beanie's ``set()`` merges the updated document back onto ``site``, so the
    # response below is built from the list INCLUDING this receipt. That is
    # load-bearing and not obvious at the call site: an explicit local mirror was
    # written here first, then mutation-tested away as dead.
    await site.set({"client_invoices": [inv.model_dump() for inv in kept]})

    # no-event: see update_site_client. Recording a receipt moves no money and
    # nothing downstream subscribes to it.
    return _client_response(site)


async def publish_pocket(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    name: str = "",
    site_plan_key: str | None = None,
    builder_origin: str | None = None,
    prewarm_origin: str | None = None,
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

    ``prewarm_origin`` is the origin the background native-artifact pre-warm should
    build with — SEPARATE from ``builder_origin`` so it steers ONLY the pre-warmed
    armed artifact, never the PUBLIC deploy (a live ``/sites/publish`` stays plain).
    The REST ``/sites/publish`` router passes the request ``Origin`` header here so
    the pre-warm produces the SAME content hash a browser's ``GET /native-artifact``
    view (which resolves origin from its own request Origin header) will ask for;
    otherwise the pre-warm would fall back to ``PAW_SITES_BUILDER_ORIGIN`` while the
    view uses the dashboard origin, so their hashes never match and every view is a
    cold miss. It defaults to ``None`` (chat-agent / MCP callers have no request
    origin, so the pre-warm keeps the env fallback — set PAW_SITES_BUILDER_ORIGIN to
    the dashboard origin in deployments so that fallback matches too).

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
    # MT-1: the pocket's authored declaration that its own client JS is
    # load-bearing. Rides ``siteConfig.keepsClientBundle`` to the generator, which
    # then emits ``csr = true`` instead of the static default. camelCase because
    # that is the pocket WIRE dict (``keeps_client_bundle`` is the Beanie/domain
    # name).
    #
    # THIS IS THE ONE PLACE the tri-state collapses to a bool
    # (feat/sites-js-by-default). ``None`` — the author declared nothing, which
    # includes every legacy pocket — resolves to the
    # ``sites_keep_client_bundle_default`` setting, ``True`` by default: sites
    # ship their own JavaScript unless told otherwise. An EXPLICIT ``True`` or
    # ``False`` is an authorial decision and beats the setting in both
    # directions, so a site that declares ``False`` still gets no bundle no
    # matter how the default is set. Everything downstream — ``publish``,
    # ``generator_client.build``, the deferred-activation snapshot — keeps
    # receiving a plain resolved ``bool``, so no other signature changes.
    # Local import, matching this module's existing ``get_settings`` use in
    # ``_billing_provider`` — the top of the file deliberately imports no
    # ``pocketpaw.config``.
    from pocketpaw.config import get_settings

    _declared = pocket.get("keepsClientBundle")
    keeps_client_bundle = (
        get_settings().sites_keep_client_bundle_default if _declared is None else bool(_declared)
    )

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
            keeps_client_bundle=keeps_client_bundle,
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
            keeps_client_bundle=keeps_client_bundle,
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
        keeps_client_bundle=keeps_client_bundle,
        preview=False,
        _generator=_generator,
        _cloudflare=_cloudflare,
        _bundle_reader=_bundle_reader,
        _local_deploy=_local_deploy,
    )

    # feat/sites-native-artifact-no-build: a LIVE svelte publish pre-warms the native
    # artifact cache in the BACKGROUND so the FIRST preview after publish is a
    # read-through HIT instead of an on-view build. The live publish itself deploys the
    # PLAIN (non-armed) public site (the /sites/publish endpoint threads no
    # builder_origin — see the armed-vs-plain finding in the PR); the pre-warm produces
    # the ARMED artifact the native editor needs, so the public deploy is unchanged.
    #
    # ORIGIN-STABILITY (fix/sites-prewarm-origin): the pre-warm must build with the SAME
    # origin the browser's native-artifact VIEW resolves — the request Origin header —
    # or the content hashes never match and the pre-warmed artifact is dead weight. So
    # steer the pre-warm by ``prewarm_origin`` (the request Origin the /sites/publish
    # router threads), falling back to ``builder_origin`` (an armed publish) and then —
    # inside the pre-warm — the PAW_SITES_BUILDER_ORIGIN env. This does NOT touch the
    # public deploy above (still plain on the REST path); it only picks the arm origin.
    #
    # Only svelte sites have a native build; a dynamic site deferred to the provision
    # job returns deployed=False, so guard on doc.deployed too. Best-effort, off the
    # publish's path. Forwards the injected generator so a faked publish warms with the
    # same fake (unit tests never shell out).
    if engine == "svelte" and getattr(doc, "deployed", False):
        _schedule_native_prewarm(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            builder_origin=prewarm_origin or builder_origin,
            _generator=_generator,
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
    keeps_client_bundle: bool,
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
        # MT-1 — MUST be captured here. This dict is the complete record of what a
        # deferred deploy replays; anything the publish path reads that is missing
        # here is silently lost when the ``subscription.active`` webhook deploys,
        # and a paid interactive site would go live with its JavaScript stripped.
        "keeps_client_bundle": keeps_client_bundle,
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
        # MT-1 — replay the authored declaration. A pending doc captured before
        # this field existed has no key and reads False (the prior behaviour).
        keeps_client_bundle=bool(inputs.get("keeps_client_bundle")),
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
    prewarm_origin: str | None = None,
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

    ``prewarm_origin`` is the origin the background native-artifact pre-warm should
    build with — the leaf-edits router passes the request ``Origin`` header so the
    pre-warm produces the SAME content hash the browser's native-artifact VIEW
    (which resolves origin from its own request Origin header) will ask for. Without
    it the pre-warm falls back to ``PAW_SITES_BUILDER_ORIGIN`` while the view uses the
    dashboard origin — different hash, so the warmed artifact is never hit. Defaults
    to ``None`` (the pre-warm keeps the env fallback for callers with no request
    origin).

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
    changed = False
    for path, contents in new_map.items():
        if path not in files:
            continue
        if files.get(path) != contents:
            await pockets_service.set_svelte_source_file(
                pocket_id, user_id, component_path=path, new_source=contents
            )
            changed = True

    # feat/sites-native-artifact-no-build: source changed → pre-warm the native
    # artifact cache in the BACKGROUND so the next preview/arm is a read-through HIT
    # instead of an on-view build. Only when something actually changed (a fully
    # rejected batch persists nothing, so it warms nothing). Best-effort + off the
    # edit's path; a pre-warm failure never affects this call.
    #
    # ORIGIN-STABILITY (fix/sites-prewarm-origin): warm with the request Origin the
    # native editor called from (``prewarm_origin``) so the hash matches the browser's
    # native-artifact view; ``None`` keeps the pre-warm's PAW_SITES_BUILDER_ORIGIN env
    # fallback for callers with no request origin.
    if changed:
        _schedule_native_prewarm(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            builder_origin=prewarm_origin,
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
    _store: Any | None = None,
) -> dict[str, str]:
    """Serve a svelte Paw Site's ARMED render as ``{pocket_id, body_html, css}`` so the
    native editor can shadow-render it (NE-5b) instead of framing an iframe.

    READ-THROUGH cache (feat/sites-native-artifact-no-build). Viewing a site must NOT
    trigger a build — the prior behaviour ran a full SvelteKit build on EVERY call
    (1-2 min on the prod box). Now:
      1. read the pocket (the pockets service's PUBLIC ``get`` raises NotFound /
         Forbidden itself — a missing / cross-tenant pocket surfaces as 404 / 403) and
         assert it is a svelte site (else ``ValidationError`` → 422), mirroring
         ``apply_leaf_edits`` / ``edit_svelte_component``;
      2. resolve the ARM inputs (source map, theme, builder origin — defaulting to the
         configured ``PAW_SITES_BUILDER_ORIGIN`` when the caller passes none, exactly
         like ``make_site_editable``) and hash them with the generator version into a
         content hash;
      3. CACHE HIT — the store already holds that render → return it from disk with
         ZERO subprocess builds (the whole point). Publish and the post-edit pre-warm
         populate the store ahead of the view, so a live/clean site is a hit;
      4. CACHE MISS — build ONCE (armed: ``builder_origin`` set so the generator stamps
         ``data-uid`` + embeds ``paw-edit-manifest``; ``smoke=False`` arm gate) through
         ``_build_or_cloud_error`` (a toolchain / non-zero / SmokeGate failure becomes a
         clean CloudError, not an opaque 500), read the built ``<body>`` inner HTML +
         concatenated CSS, STORE it, and return. PERF-3's stable per-pocket build dir
         keeps node_modules / bun install cached across builds.

    Local-mode degrade: a store miss on a box where no build has ever run just takes
    the MISS branch (build once); a build failure still maps to a clean CloudError, so
    the endpoint degrades cleanly rather than 500ing opaquely — unchanged from before.

    ``_generator`` (defaults to a real ``GeneratorClient``), ``_read_built`` (defaults
    to ``_read_native_artifact`` — the disk read + HTML/CSS extraction), and ``_store``
    (defaults to the filesystem artifact store) are injectable seams so the path is
    unit-testable without Bun / a real build / disk."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import generator_client

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
    store = _store or _default_artifact_store()
    content_hash = _artifact_content_hash(
        source=source,
        theme=theme,
        builder_origin=origin,
        gen_version=generator_client.generator_version(),
    )
    # READ-THROUGH: a hit serves the prior render straight off disk — no generator, no
    # subprocess, no build. This is what makes a VIEW instant on the prod box.
    cached = store.read(pocket_id, content_hash)
    if cached is not None:
        body_html, css = cached
        return {"pocket_id": pocket_id, "body_html": body_html, "css": css}

    # MISS: build once (armed), cache, return. ``_build_native_artifact`` routes through
    # _build_or_cloud_error so a missing-toolchain / non-zero build / SmokeGateFailed
    # becomes a clean CloudError (sites.generator_failed → 5xx), not an opaque 500.
    generator = _generator or GeneratorClient()
    read = _read_built or _read_native_artifact
    body_html, css = await _build_native_artifact(
        generator=generator,
        theme=theme,
        source=source,
        site_name=site_name,
        builder_origin=origin,
        pocket_id=pocket_id,
        read=read,
    )
    store.write(pocket_id, content_hash, body_html, css)
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
        doc = await publish_pocket(
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

    # feat/sites-native-artifact-no-build: the component source changed → pre-warm the
    # native artifact cache in the BACKGROUND (re-applying the SE-2b builder origin) so
    # the next native shadow-render is a read-through HIT. Best-effort, off this call's
    # path; fired only after the preview republish above succeeded (no arm on rollback).
    _schedule_native_prewarm(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        builder_origin=builder_origin or None,
        _generator=_generator,
    )
    return doc


async def edit_react_component(
    *,
    user_id: str,
    pocket_id: str,
    component_path: str,
    new_source: str | None = None,
    edits: list[dict[str, str]] | None = None,
    create: bool = False,
    _pockets: Any = None,
) -> dict[str, Any]:
    """Write ONE file of a react Paw Site pocket and leave it as a reviewable DRAFT.

    The chat-agent entry point for a targeted react edit (RX-3). Before this
    existed the react track had no edit path: ``edit_svelte_component`` rejects a
    react pocket, so the agent's only move on "shorten the hero headline" was a
    second ``create_react_site`` — a SECOND site pocket instead of a change to the
    one the user is looking at.

    The edit is expressed one of two ways (exactly one is required), the same
    contract ``edit_svelte_component`` uses so the agent's instinct transfers:

      * ``edits`` — a list of ``[{old_string, new_string}, ...]`` search/replace
        blocks applied to the file's CURRENT contents via the shared
        :func:`apply_edits`, which requires each ``old_string`` to match exactly
        once and raises a clear, retry-able ``ValidationError`` otherwise. PREFERRED
        for small changes: the agent emits only the diff.
      * ``new_source`` — the FULL new file contents, used as-is. For large rewrites,
        and the ONLY form accepted with ``create=True`` (there is nothing to diff
        against a file that does not exist yet).

    ``create=True`` mints a NEW path instead of editing an existing one. It exists
    because "add a testimonials section" needs a new component file PLUS an edit to
    ``src/App.tsx``, and without it the agent cannot add a section at all. It
    INVERTS the existence check rather than relaxing it — ``create=False`` requires
    the path to exist (a typo is never a silent create), ``create=True`` requires it
    NOT to (an accidental overwrite of a real component is worse than a rejected
    call).

    **DRAFT-ONLY. This does NOT republish and does NOT enqueue a build**, and that
    is a deliberate departure from ``edit_svelte_component``, which republishes and
    rolls the source back when the workerd smoke gate rejects the edit. That
    contract cannot transfer: ``build_runs_async("react")`` is True, so a react
    publish ENQUEUES a Daytona build and returns before any build outcome exists —
    there is nothing synchronous to roll back from, and a rollback fired on an
    enqueue-success would revert a good edit. So persisting the edited draft IS the
    whole job, the same shape ``apply_leaf_edits`` already documents. Publishing
    stays the user's call (what ``pocketpaw-create-react-site`` STEP 4 promises).
    A future reader looking at this and reaching for the missing republish should
    read this paragraph first.

    The path guard is load-bearing, not hygiene. ``create_react_site`` refuses
    generator-owned paths, and an edit that did not would be a way around that
    allowlist: ``edit_react_component(component_path="package.json", create=True)``
    writes the dependency manifest, defeating the generator's dependency allowlist
    and with it the supply-chain release-age floor the manifest is what enforces.
    Both guards call the SAME
    :func:`pocketpaw_ee.sites.react_paths.react_path_rejection` — normalized, so
    ``./package.json`` and ``src/paw/../paw/entry.tsx`` cannot spell their way past
    it — and it also requires the resolved path to land under ``src/`` or
    ``public/``.

    Raises, all BEFORE anything is written:
      * ``ValidationError("site_edit.invalid_args")`` — not exactly one of
        ``edits`` / ``new_source``;
      * ``ValidationError("site_edit.create_needs_source")`` — ``create=True``
        without ``new_source``;
      * ``ValidationError("site_edit.reserved_path")`` — a generator-owned path;
      * ``ValidationError("site_edit.path_outside_source")`` — outside
        ``src/`` / ``public/``;
      * ``ValidationError("pocket.not_react_site")`` — not a react site pocket;
      * ``NotFound("site_component")`` — ``create=False`` and the path is absent;
      * ``ValidationError("pocket.react_component_exists")`` — ``create=True`` and
        the path is present;
      * whatever :func:`apply_edits` raises on a 0-match / ambiguous ``old_string``.

    ``_pockets`` is an injectable seam for the pockets service so the orchestration
    is unit-testable with no Bun / workerd / Cloudflare in sight — there is nothing
    else to inject, because there is no build.

    Takes NO ``workspace_id``, unlike its siblings: tenancy is resolved by the
    pockets service off ``user_id`` (its public ``get`` raises Forbidden itself), and
    ``edit_svelte_component`` only needs the workspace to look up the Site doc whose
    builder origin its republish must re-apply — a republish this lane does not do.

    Returns ``{pocket_id, component_path, created}``.
    """
    if _pockets is not None:
        pockets_service = _pockets
    else:
        from pocketpaw_ee.cloud.pockets import service as pockets_service  # type: ignore[no-redef]

    # Exactly one of the two edit shapes (mirrors edit_svelte_component).
    if (edits is None) == (new_source is None):
        raise ValidationError(
            "site_edit.invalid_args",
            "edit_react_component requires exactly one of `edits` (a targeted "
            "search/replace diff) or `new_source` (a full file rewrite).",
        )
    if create and new_source is None:
        raise ValidationError(
            "site_edit.create_needs_source",
            "Creating a new component needs `new_source` — the full contents of the "
            "new file. There is nothing for `edits` to search against.",
        )

    # The reserved-path guard. FIRST, before the pocket is even read: a call that
    # names a generator-owned path is rejected on the path alone, so no amount of
    # pocket state can make it land.
    if (reason := react_path_rejection(component_path)) is not None:
        code = (
            "site_edit.reserved_path"
            if is_reserved_react_path(component_path)
            else "site_edit.path_outside_source"
        )
        raise ValidationError(code, reason)

    # The pockets service's PUBLIC get raises NotFound / Forbidden itself (entity
    # isolation) — a missing / cross-tenant pocket surfaces as 404 / 403.
    pocket = await pockets_service.get(pocket_id, user_id)
    if (pocket.get("engine") or "ripple") != "react" or not isinstance(pocket.get("source"), dict):
        raise ValidationError(
            "pocket.not_react_site",
            "This pocket is not a react Paw Site — it has no component source map to edit.",
        )
    source_map = pocket["source"]
    # The MISSING-path half of the inversion is enforced here as well as at the write,
    # because the ``edits`` branch below indexes the map: without it a typo'd path is a
    # KeyError instead of the NotFound the caller has to relay. The EXISTING-path half
    # (``create`` onto a live component) is deliberately NOT duplicated — nothing here
    # reads the file on the create path, so the check would only be a second copy of a
    # rule the write chokepoint already owns for every caller. Proven rather than
    # assumed: with the duplicate present, the mutation that deleted it ESCAPED, because
    # the chokepoint caught the write anyway.
    if not create and component_path not in source_map:
        raise NotFound("site_component", component_path)

    if edits is not None:
        # apply_edits raises ValidationError (clear, retry-able) on any match-count
        # violation, BEFORE anything is persisted.
        new_source = apply_edits(source_map[component_path], edits)

    assert new_source is not None  # narrowing: the arg checks above guarantee it
    await pockets_service.set_react_source_file(
        pocket_id,
        user_id,
        component_path=component_path,
        new_source=new_source,
        create=create,
    )

    # NO publish, NO enqueue, NO native pre-warm. The pre-warm serves the svelte
    # native editor's shadow-render, which reads a SvelteKit build; react has no
    # such artifact, so warming one here would build a site nothing reads.
    return {"pocket_id": pocket_id, "component_path": component_path, "created": create}


async def edit_html_file(
    *,
    user_id: str,
    pocket_id: str,
    file_path: str,
    new_source: str | None = None,
    edits: list[dict[str, str]] | None = None,
    create: bool = False,
    _pockets: Any = None,
) -> dict[str, Any]:
    """Write ONE file of an html Paw Site pocket and leave it as a reviewable DRAFT.

    The chat-agent entry point for a targeted html edit (HE-10), and the third and
    last engine to get one. Before this the html track had NO chat edit path at
    all: ``edit_svelte_component`` raises ``pocket.not_svelte_site`` on an html
    pocket and ``edit_react_component`` raises ``pocket.not_react_site``, so the
    agent's only move on "change the phone number in the footer" was a second
    ``create_html_site`` — a SECOND site pocket, at a second URL, instead of a
    change to the one the user is looking at. That is the exact hole RX-3 closed
    for react, reopened one engine over.

    **This is a FILE edit, not a component edit, and the name says so.** svelte and
    react both have a component model, so their tools take a ``component_path``. An
    html site has none: ``html-scaffold.ts`` writes the author's map verbatim into
    the directory the edge serves, so what exists is files — ``index.html``,
    ``css/site.css``, ``about/index.html``. Calling the parameter
    ``component_path`` here would have been consistency bought by lying about the
    content model.

    The edit is expressed one of two ways (exactly one is required), the same
    contract both siblings use so the agent's instinct transfers:

      * ``edits`` — a list of ``[{old_string, new_string}, ...]`` search/replace
        blocks applied to the file's CURRENT contents via the shared
        :func:`apply_edits`, which requires each ``old_string`` to match exactly
        once and raises a clear, retry-able ``ValidationError`` otherwise. PREFERRED
        for small changes: the agent emits only the diff. On this track that saving
        is at its largest — an html page is one flat document with no component
        decomposition, so a "full rewrite" means re-emitting the entire site page.
      * ``new_source`` — the FULL new file contents, used as-is. For large rewrites,
        and the ONLY form accepted with ``create=True`` (there is nothing to diff
        against a file that does not exist yet).

    ``create=True`` mints a NEW path instead of editing an existing one — "add an
    about page" needs ``about.html`` PLUS a link from ``index.html``. It INVERTS the
    existence check rather than relaxing it (``create=False`` requires the path to
    exist, ``create=True`` requires it not to), so exactly one of the two mistakes
    is impossible in each mode.

    **DRAFT-ONLY. This does NOT republish**, matching ``edit_react_component`` and
    ``apply_leaf_edits`` rather than ``edit_svelte_component``. The svelte tool
    republishes because it has a workerd smoke gate to catch a broken edit and a
    rollback to run when the gate fires. html has NEITHER: ``needs_node_build``
    is False for html, so there is no build, no smoke render, and nothing that can
    reject an edit before it deploys. A republish here would therefore push
    unvalidated markup straight to a live customer site with no gate in between —
    strictly worse than leaving it as a draft the user publishes deliberately. So
    persisting the edited draft IS the whole job, and publishing stays the user's
    call. A future reader looking at this and reaching for the missing republish
    should read this paragraph first.

    The path guard is load-bearing, not hygiene, and it is html's own — see
    :mod:`pocketpaw_ee.sites.html_paths` for why react's rules could not be reused
    (an html site's files legitimately live at the project ROOT, which react's
    ``src/``-or-``public/`` rule would reject outright). Two rejections: a path that
    escapes the site directory, and the generator-owned ``_paw/`` namespace, where
    ``_paw/edit-manifest.json`` maps each editable element to a byte range. An
    author who could shadow that manifest would not break anything loudly — the
    next NATIVE editor edit would splice at wrong offsets, landing mid-tag.

    Raises, all BEFORE anything is written:
      * ``ValidationError("site_edit.invalid_args")`` — not exactly one of
        ``edits`` / ``new_source``;
      * ``ValidationError("site_edit.create_needs_source")`` — ``create=True``
        without ``new_source``;
      * ``ValidationError("site_edit.reserved_path")`` — the ``_paw/`` namespace;
      * ``ValidationError("site_edit.path_outside_source")`` — escapes the site dir;
      * ``ValidationError("pocket.not_html_site")`` — not an html site pocket;
      * ``NotFound("site_component")`` — ``create=False`` and the path is absent;
      * ``ValidationError("pocket.html_file_exists")`` — ``create=True`` and the
        path is present;
      * whatever :func:`apply_edits` raises on a 0-match / ambiguous ``old_string``.

    ``_pockets`` is an injectable seam for the pockets service so the orchestration
    is unit-testable with no Bun / Cloudflare in sight — there is nothing else to
    inject, because there is no build.

    Takes NO ``workspace_id``, for the same reason ``edit_react_component`` does
    not: tenancy is resolved by the pockets service off ``user_id`` (its public
    ``get`` raises Forbidden itself), and the workspace is only needed to look up
    the Site doc whose builder origin a REPUBLISH must re-apply — which this lane
    does not do.

    Returns ``{pocket_id, file_path, created}``.
    """
    if _pockets is not None:
        pockets_service = _pockets
    else:
        from pocketpaw_ee.cloud.pockets import service as pockets_service  # type: ignore[no-redef]

    # Exactly one of the two edit shapes (mirrors both siblings).
    if (edits is None) == (new_source is None):
        raise ValidationError(
            "site_edit.invalid_args",
            "edit_html_file requires exactly one of `edits` (a targeted "
            "search/replace diff) or `new_source` (a full file rewrite).",
        )
    if create and new_source is None:
        raise ValidationError(
            "site_edit.create_needs_source",
            "Creating a new file needs `new_source` — the full contents of the new "
            "file. There is nothing for `edits` to search against.",
        )

    # The path guard. FIRST, before the pocket is even read: a call that names an
    # unwritable path is rejected on the path alone, so no amount of pocket state
    # can make it land.
    if (reason := html_path_rejection(file_path)) is not None:
        code = (
            "site_edit.reserved_path"
            if is_reserved_html_path(file_path)
            else "site_edit.path_outside_source"
        )
        raise ValidationError(code, reason)

    # Store under the NORMALIZED key, not the one the agent spelled. The generator
    # normalizes before it writes (``assertSafeRelPath`` → ``join(outDir, rel)``), so
    # two keys that normalize to the same path are ONE file on disk. Persisting the
    # raw spelling would let ``create=True`` on ``./index.html`` pass the
    # not-already-present check, land a SECOND map entry, and then have both entries
    # materialize onto ``index.html`` — leaving the live home page decided by dict
    # iteration order. Normalizing here also makes ``./index.html`` and
    # ``img\\logo.svg`` resolve to the existing file the author meant, instead of
    # 404ing on a path that is present under its canonical spelling.
    file_path = normalize_html_path(file_path)

    # The pockets service's PUBLIC get raises NotFound / Forbidden itself (entity
    # isolation) — a missing / cross-tenant pocket surfaces as 404 / 403.
    pocket = await pockets_service.get(pocket_id, user_id)
    if (pocket.get("engine") or "ripple") != "html" or not isinstance(pocket.get("source"), dict):
        raise ValidationError(
            "pocket.not_html_site",
            "This pocket is not an html Paw Site — it has no raw source map to edit.",
        )
    source_map = pocket["source"]
    # The MISSING-path half of the inversion is enforced here as well as at the
    # write, because the ``edits`` branch below indexes the map: without it a typo'd
    # path is a KeyError instead of the NotFound the caller has to relay. The
    # EXISTING-path half is deliberately NOT duplicated — nothing here reads the
    # file on the create path, so the check would only be a second copy of a rule
    # the write chokepoint already owns for every caller. (Same reasoning, and the
    # same proven-by-mutation result, as ``edit_react_component``.)
    if not create and file_path not in source_map:
        raise NotFound("site_component", file_path)

    if edits is not None:
        # apply_edits raises ValidationError (clear, retry-able) on any match-count
        # violation, BEFORE anything is persisted.
        new_source = apply_edits(source_map[file_path], edits)

    assert new_source is not None  # narrowing: the arg checks above guarantee it
    await pockets_service.set_html_source_file(
        pocket_id,
        user_id,
        file_path=file_path,
        new_source=new_source,
        create=create,
    )

    # NO publish, NO enqueue, NO native pre-warm — see the DRAFT-ONLY paragraph
    # above. The pre-warm serves the svelte native editor's shadow-render, which
    # reads a SvelteKit build; html has no such artifact.
    return {"pocket_id": pocket_id, "file_path": file_path, "created": create}


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
        # SC-1: the same screenshot the list row carries, so a by-pocket status
        # read can show the page too. None when there is no deployed site, no
        # capture has landed yet, or the doc predates the field.
        preview_image_url=(getattr(doc, "preview_image_url", "") or None)
        if doc is not None
        else None,
        # SL-3: the build lane's state on the read a builder polls BY POCKET. This is
        # the only GET keyed on a pocket id, so a client watching a build it just
        # triggered has nowhere else to look — without these it would have to fetch the
        # whole gallery list to find one site's build state.
        #
        # A pocket with NO Site doc reads the "no build" defaults rather than null: a
        # draft that has never been published has not got a failed build, it has got no
        # build, and those must not look the same to a badge.
        build_status=getattr(doc, "build_status", "none") if doc is not None else "none",
        build_reason=getattr(doc, "build_reason", None) if doc is not None else None,
        build_job_id=getattr(doc, "build_job_id", None) if doc is not None else None,
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
    "create_draft_site",
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
    "remove_domain",
    "domain_status",
    "list_domains",
    "list_for_workspace",
    "site_pocket_ids",
    "reserve_local_sites",
]
