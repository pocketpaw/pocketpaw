<!-- How a paw-build.json build (project engine / base app templates) is deployed
     through the Cloudflare HTTP API (into the WfP dispatch namespace, or as an
     account-level Worker while the account has no WfP), its performance settings, and what
     gates enabling it. -->
# Sites: bundle deploys (`paw-build.json`)

Builds from the `project` engine and the base app templates (Next.js via OpenNext,
TanStack Start, Astro, ...) are not single `index.mjs` workers. They are a worker made
of one or more ES modules plus a directory of static assets. The sandbox build
describes that output in `paw-build.json`, and the API host deploys it through the
Cloudflare HTTP API, into the `paw-sites` dispatch namespace or, interim, as an
account-level Worker (see "Deploy targets" below).

Code: `ee/pocketpaw_ee/sites/bundle_deploy.py` (vetting and mapping),
`ee/pocketpaw_ee/sites/binding_provisioner.py` (the per-site D1 database, KV
namespaces and R2 buckets), `ee/pocketpaw_ee/sites/project_d1.py` (a project's D1
migrations) and `CloudflareClient.upload_assets` / `put_worker(modules=...)` in
`ee/pocketpaw_ee/sites/cloudflare_client.py` (the wire calls).

## When it runs

- In the WfP publish path, a build whose project dir contains `paw-build.json` takes the
  bundle deploy. No existing engine (ripple, svelte, react, html) writes that file, so
  their deploys are unchanged: a single-module PUT, or the one-module multipart with a
  D1 binding for dynamic sites.
- A `project` pocket publishes from its stored draft build (see "Project builds"
  below): the bundle is materialized and goes through this path. Under
  `PAW_CF_DEPLOY_MODE=workers` a project is still deployed here, through the HTTP API
  (the wrangler-based workers path would read author config on the API host), but to
  the `account` target. `local` mode serves the bundle's `assetsDir` statically.
- `sites.service.deploy_bundle(site, build_dir)` deploys a build dir directly (no
  Site-row plumbing), for callers that already hold one.

## Project builds

A `project` pocket's source map is the whole repo. It builds **only** in a Daytona
sandbox (`ee/pocketpaw_ee/sites/project_build.py`):

1. The source map is uploaded as the project tree, with the vendored generator
   (`dist/` + `package.json` from `PAW_SITES_GEN_DIR`, default `/opt/paw-sites`) beside
   it at `/tmp/paw-sites-gen`. A host without the generator fails the build as
   `sandbox_unavailable:generator_missing`.
2. `paw-sites-gen project-build --dir <project> --out <project>/paw-build.json --json`
   installs, builds and (worker targets) runs the project's own wrangler dry-run. The
   project brings its own wrangler as a devDependency.
3. A stage step copies `paw-build.json` and the files it names into `.paw/out`, which
   the build wrapper tars. Paths that leave the project, and an `assetsDir` at the
   project root, are refused.
4. The worker stores the whole bundle in the artifact store under `<hash>.bundle`
   (publish reads it), the assets alone as the draft's preview files, and a build
   record with the redacted, capped (64 KiB tail) log in the verify store.

The build budget is `PAW_SITES_BUILD_TIMEOUT_SEC_PROJECT` (else the shared knob, else
600s). Supersede, single-flight and the artifact caps are the preview lane's.

Publishing needs a finished build of the pocket's **current** files; otherwise it is a
`409 sites.project_build_required`. A first publish inserts the Site document
(undeployed) before anything is provisioned, so the D1, KV and R2 it creates are
recorded on it. Before the deploy the plan gate runs
(captain decision 2026-10-07): a free site may ship static output, and a worker that
binds nothing beyond D1, KV and its own assets. Any other binding, or server code on a
site with a custom domain, is refused with `422 sites.server_code_not_entitled` and an
upgrade message. R2 and the rest of the binding rules then apply as below.

The image ships `starters/` and `recipes/` next to the generator (`Dockerfile.enterprise`
copies them, `scripts/vendor-paw-sites.sh` vendors them with `git archive`), for
`template-copy` and `apply-recipe`.

## The manifest

The API host reads only `paw-build.json` and the files it names. It never reads a
wrangler config and never runs a command (author config on the API host is RCE and
can point at other tenants' resources). Fields used:

| Field | Use |
|---|---|
| `workerModuleDir` | The wrangler dry-run outdir (`.paw/worker`). Module part names are paths relative to it. |
| `mainModule` | Part name of the main module (metadata `main_module`). May contain a slash. |
| `workerEntry` | `<workerModuleDir>/<mainModule>`. Older manifests without the two fields above fall back to it. |
| `workerModules` | Module files, any extension, relative to the build root (or to the module dir). `*.map` and `README.md` are skipped. Empty for an assets-only build. |
| `assetsDir` | Static assets directory. Every file is uploaded except `_headers`, `_redirects`, `.assetsignore` and what `.assetsignore` matches. Assets upload whether or not an `assets` binding is requested. |
| `assetsConfig` | `_headers` / `_redirects` strings (null when absent; lifted from `assetsDir` for older manifests), plus `html_handling`, `not_found_handling`, `run_worker_first`. They go to the upload's `assets.config`. Other keys are dropped. |
| `compat` | `{date, flags}` (wrangler's `compatibility_*` names are accepted too). |
| `bindingRequests` | `{type, name, required?}`. Only these three keys are read. |
| `requiredSecrets` | Optional list of secret names the site cannot run without (also accepted as `required_secrets`). Adds to `secret` requests marked `required`. |
| `droppedBindings` | Bindings the build refused. Logged as warnings. |

The shape is paw-sites' `buildPawManifest` (`src/starters.ts`, documented in
`starters/README.md`). Unknown fields (`sizes`, `startup`, `framework`, ...) are
ignored, so paw-sites can add fields without a pocketpaw release.

## Deploy targets

The bundle path has two targets. Everything in this document (manifest parsing,
binding mapping, compat allow-list, secrets, size caps, provisioning, D1 migrations)
applies to both. Only the API URLs and how the site is served differ.

| Target | Script URL | Served at | Picked when |
|---|---|---|---|
| `dispatch` | `/accounts/{acct}/workers/dispatch/namespaces/{ns}/scripts/{site_id}` | `https://{site_id}.{PAW_CF_SITES_DOMAIN}` through the dispatch worker | `PAW_CF_DEPLOY_MODE=wfp` (or unset) |
| `account` | `/accounts/{acct}/workers/scripts/{worker_name}` | `https://{worker_name}.{sub}.workers.dev`, plus custom-domain routes | `PAW_CF_DEPLOY_MODE=workers` |

`PAW_SITES_PROJECT_DEPLOY_TARGET=account|dispatch` overrides the mode for project
bundles. An unknown value is logged and ignored.

### Option B: account-level Workers (interim)

The production Cloudflare account has no Workers for Platforms, so every dispatch
upload fails with `403 ... dispatch namespaces (code 10121)`. Until WfP is bought,
project bundles in workers mode deploy as **account-level Workers** through the HTTP
API (never wrangler), and are served exactly like the wrangler-built sites in that
mode:

- **Name.** The site's workers-mode Worker name (`workers_deploy.site_worker_name`):
  the name-based slug claimed on first publish, else `paw-site-<site_id>`. Renames
  (`slug_pending`), the foreign-script guard and the 3-a-day limit apply unchanged.
- **Address.** After the PUT, `POST /accounts/{acct}/workers/scripts/{name}/subdomain`
  with `{"enabled": true, "previews_enabled": false}` turns on workers.dev (an API
  upload leaves it off; wrangler sends the same call for `workers_dev: true`). The URL
  is `https://{name}.{sub}.workers.dev`, where `{sub}` is `PAW_CF_WORKERS_SUBDOMAIN`
  or, when unset, `GET /accounts/{acct}/workers/subdomain`.
- **Row.** `deploy_target` is stamped `workers`, so custom domains get a
  `{hostname}/*` Worker route to this script and a site delete calls
  `DELETE /accounts/{acct}/workers/scripts/{name}`.
- **Badge and concierge.** Stamped into the built pages before the upload, the same
  as every engine.
- **Not applied.** The pageview counter (it wraps the Worker entry through wrangler;
  a project's multi-module Worker is uploaded as built, so the site records no
  analytics) and the AI-ready files (robots.txt, sitemap, llms.txt, IndexNow; they
  are written into a wrangler asset dir). Both are skipped, never half-applied.

Risks, and why this is interim:

- **No dispatch isolation.** A tenant's Worker is one of the account's own scripts.
  It shares the account's script count limit and its workers.dev subdomain with our
  own Workers, and nothing sits in front of it (no dispatch worker to enforce
  outbound rules, limits or tags). The binding mapping still only hands it the
  site's own D1 / KV / R2 and secrets.
- **Account limits.** The script count limit applies to all sites together. Each
  site's own upload carries per-request CPU and subrequest caps by plan (see
  "Performance"), which is the only per-tenant limit on this target.
- **Asset store.** Hashes stay salted per workspace, as on WfP.
- Each deploy logs `deploying as an ACCOUNT-LEVEL Worker (interim, no dispatch
  isolation)`.

**Switching to WfP.** Buy Workers for Platforms on the account, create the `paw-sites`
dispatch namespace (or set `PAW_CF_DISPATCH_NAMESPACE`), deploy the dispatch worker
(`ee/pocketpaw_ee/sites/cloudflare/dispatch-worker`) on `PAW_CF_SITES_DOMAIN`, then
either set `PAW_CF_DEPLOY_MODE=wfp` or keep workers mode for the other engines and
set `PAW_SITES_PROJECT_DEPLOY_TARGET=dispatch`. A site already live as an account
Worker keeps serving there until it is republished; after the republish delete its
old account script and routes by hand (its row then says `wfp`).

**Token scopes for the account target.** `Workers Scripts Edit` covers the script
PUT, the assets session and the workers.dev toggle (`Workers Scripts Read` is enough
for the subdomain lookup). Custom domains need what workers mode already needs:
`Workers Routes Edit` on the zone (the `{hostname}/*` route) and `SSL and
Certificates Edit` (Cloudflare for SaaS custom hostnames). D1 / KV / R2 scopes are as
below.

## Upload sequence

The same three calls wrangler makes for a WfP deploy (captured in the
2026-10-07 next-on-wfp spike). The `account` target makes the same calls with
`/workers/scripts/{worker_name}` in place of
`/workers/dispatch/namespaces/{ns}/scripts/{site_id}`
([direct upload](https://developers.cloudflare.com/workers/static-assets/direct-upload/),
[script upload](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/methods/update/)),
then enables workers.dev:

1. `POST /accounts/{acct}/workers/dispatch/namespaces/{ns}/scripts/{site_id}/assets-upload-session`
   with `{"manifest": {"/path": {"hash": <32 hex>, "size": n}}}`. Cloudflare returns a
   session JWT and the `buckets` of hashes it still needs.
2. `POST /accounts/{acct}/workers/assets/upload?base64=true`, one call per bucket,
   `Authorization: Bearer <session jwt>` (not the account token), multipart with one
   base64 part per hash. The last response carries the completion JWT. If there are
   no buckets, the session JWT is the completion JWT.
3. `PUT /accounts/{acct}/workers/dispatch/namespaces/{ns}/scripts/{site_id}`, multipart:
   - `metadata`: `{main_module, bindings, compatibility_date, compatibility_flags,
     assets: {jwt, config}}`, plus `observability`, `limits` and (opt-in)
     `placement` for a bundle with worker modules (see "Performance" below)
   - one part per module, named by its path, typed by extension: `.js`/`.mjs`
     `application/javascript+module`, `.cjs` `application/javascript`, `.wasm`
     `application/wasm`, `.json` `application/json`, `.txt`/`.html`/`.md`/`.sql`
     `text/plain`, anything else `application/octet-stream`.

Every check runs before step 1, so a refused bundle leaves the live site untouched.
Asset hashes are SHA-256 over the workspace id, the bytes and the extension, cut to
32 hex. Assets are shared and deduplicated across a namespace, so the per-tenant salt
stops one tenant probing whether another uploaded a given file.

## What is enforced

- **Compat flags:** only `nodejs_compat`, `nodejs_compat_v2`, `nodejs_als` and
  `global_fetch_strictly_public`. Anything else is dropped with a warning.
- **Compat date:** the author's date. When `nodejs_compat` is set, a date before
  `2024-09-23` is bumped to it. Future dates are clamped to today, and a missing or
  invalid date becomes `2026-09-01`.
- **Bindings:** `assets` maps to the uploaded assets under the requested name. `d1`,
  `kv` and `r2` map to resources we provisioned for the site (see "Backend bindings"
  below). `do`, `queues` and `ai` are refused as not supported yet. Secrets come
  from the per-site store (see "Secrets" below); a missing required secret refuses
  the deploy.
  `service`, `dispatch_namespaces`, `tail_consumers`, `images`, routes and unknown
  types are never forwarded. Author ids are never read.
- **Limits:** modules over 64 MiB uncompressed in total
  ([Workers limits](https://developers.cloudflare.com/workers/platform/limits/)).
  Cloudflare documents no module-count limit, so we cap at 1,000 ourselves. Assets:
  100,000 files, 25 MiB per file.

## Backend bindings

Only `type` and `name` are read from a request. The resource behind it is ours,
created in our account and recorded on the Site document; an id, namespace or
bucket the author wrote is ignored.

| Request | Upload binding | Resource | Plan |
|---|---|---|---|
| `d1` | `{type: "d1", name, id}` | The site's own D1 (`Site.d1_database_id`), named `paw-site-<siteid>`. Created on the first publish that binds it, or by the dynamic-site provision job. One per site. | Free and up |
| `kv` | `{type: "kv_namespace", name, namespace_id}` | One namespace per binding name (`Site.kv_namespaces`). | Free: 1 per site. Site tier and up: 3. |
| `r2` | `{type: "r2_bucket", name, bucket_name}` | One bucket per binding name (`Site.r2_buckets`). | Site tier and up: 3 per site. Refused on free. |
| `do` | `{type: "durable_object_namespace", name, class_name}` | Nothing created: Cloudflare makes the namespace when the migration applies. See "Durable Objects". Behind `PAW_SITES_DURABLE_OBJECTS`; refused while it is off. | Free: 1 class. Paid: `PAW_SITES_DO_MAX_CLASSES`. |
| `queues`, `ai` | none | Refused: "not supported on Paw Sites yet". | |

**Plan gating.** Free gets D1 and KV under tight limits; R2 (and later Durable
Objects and bring-your-own backends) needs the `site` tier or above with an active
subscription. The rule is `entitlements.site_paid_backends_entitled`, the same tier
set as the code download (`site`, `staff` and their yearly partner twins, legacy
`pro`/`business` included). A plan-carried site is `staff` and passes. A refused
request fails the deploy with a sentence naming the binding and the plan it needs.

**Caps** are per site and set by env: `PAW_SITES_MAX_KV_NAMESPACES_FREE` (default 1),
`PAW_SITES_MAX_KV_NAMESPACES` (default 3, paid) and `PAW_SITES_MAX_R2_BUCKETS`
(default 3, paid). They count what the site already has plus what the build asks
for, so renaming a binding on a site at its cap is refused until the old one is
removed. KV is the scarce one: Cloudflare allows 1,000 namespaces per account
([KV limits](https://developers.cloudflare.com/kv/platform/limits/)). R2 allows
1,000,000 buckets per account ([R2 limits](https://developers.cloudflare.com/r2/platform/limits/)).

**Names.** `paw-<siteid>-<binding slug>-<6 hex of sha256(binding name)>`, lowercase,
at most 63 characters, so it is a valid R2 bucket name (3 to 63 characters, `a-z`,
`0-9` and `-`, no leading or trailing hyphen:
[R2 bucket names](https://developers.cloudflare.com/r2/buckets/create-buckets/)).
The KV namespace title is the same string. The hash keeps `MY_KV` and `my_kv` apart.

**Idempotent.** A resource recorded on the site is reused with no Cloudflare call.
An unrecorded one is looked up by its derived name first (a database by exact name in
`GET /d1/database?name=`, a namespace by title in the namespace list, a bucket with
`GET /r2/buckets/{name}`), so a create that died before the save is found rather than
duplicated. A stored D1 id equal to the placeholder older publishes derived from the
workspace and pocket was never created on Cloudflare, so it is replaced by a real
database. The doc is saved after each create.
Gates and caps are checked before the first create; module, asset and compat checks
run before provisioning, so a bundle refused for those creates nothing.

**Teardown.** The delete cascade's `bindings` step runs after `d1` and before the
public-asset `r2` purge. It deletes every recorded namespace and bucket, best effort:
each failure is logged with the resource name and the rest still run, and the ledger
records `partial-needs-operator` instead of `done`. A 404 counts as deleted.
Cloudflare only deletes an empty bucket and its REST API cannot list or delete
objects, so a bucket that still holds objects gets a lifecycle rule expiring every
object after a day, and an operator deletes it after that.

**Token scopes.** Provisioning and teardown need `Workers KV Storage Write` and
`Workers R2 Storage Write` (and `D1 Edit` for project databases) on
`PAW_CF_API_TOKEN`, in addition to the Workers scripts
scope the deploy already uses.

## Durable Objects

Behind `PAW_SITES_DURABLE_OBJECTS=1`; turn it on only after the staging spike's
scenario E passes (see "The staging spike" below). With it off, a build that declares Durable
Objects (a `durableObjects` block in `paw-build.json` or a `do` binding request) is
refused with `sites.do_disabled`, and so is a site that already has live DOs; nothing
deploys without them. Code: `ee/pocketpaw_ee/sites/durable_objects.py` (vetting,
migration plan, teardown) and `ee/pocketpaw_ee/sites/do_metering.py` (sweeps). Design:
paw-workspace `docs/design/drafts/2026-10-08-sites-durable-objects.md`.

**What a build may declare.** SQLite-backed classes in the site's own script only:
migration steps carry `new_sqlite_classes`, `renamed_classes` and `deleted_classes`,
nothing else (`new_classes`, `transferred_classes` and any `script_name` /
`environment` / `namespace_id` / `dispatch_namespace` on a binding are refused). Every
bound and live class must be exported by the main module. Migrations use Cloudflare's
tagged form (`old_tag` / `new_tag` / `steps`) and are append-only.

**State.** `Site.do_migration_tags` (applied tag history, oldest first) and
`Site.do_classes`, the same two on the draft registry row. Written only after a
successful upload, from the `migration_tag` Cloudflare returns. A publish sends only
the steps after the last applied tag, with `old_tag`, so Cloudflare rejects a stale or
concurrent publish. A history that does not start with what was applied is refused
(`sites.do_history_diverged`); an older bundle is accepted as a rollback only when its
tags are a prefix of the history and it still exports every live class.
Before planning, a publish reconciles that state with Cloudflare (the script's
`migration_tag` and its DO bindings, `durable_objects.reconcile_state`): if they
disagree, Cloudflare wins and every class it binds counts as live, so a lost save can
never let a delete step skip confirmation. An unreadable answer refuses the publish
(`sites.do_state_unknown`) before anything changes. Saving the state after an upload
retries three times, then leaves it to the next reconcile.

**Destructive steps.** A pending `deleted_classes` / `renamed_classes` step on a live
class is refused with `sites.do_data_loss_unconfirmed` naming the classes, until the
publish carries `confirm_do_data_loss: ["Class", ...]` (`POST /sites/publish`, from
the owner's dialog only). The refusal carries `details: {"classes": [...]}`. A
non-empty confirmation needs the `sites.confirm_data_loss` action (workspace admin or
owner); a member gets 403 `sites.data_loss_confirm_forbidden`. Drafts allow destructive
steps without confirmation, and a draft whose history diverged tears its script down
and rotates to a fresh `paw-draft-*` name, at most
`PAW_SITES_DO_DRAFT_ROTATIONS_PER_HOUR` times per pocket per hour; past that the
preview falls back with the `draft_worker:do_rotation_limit` rung.

**Platform wrapper (enforcement).** The recipe's caps live in code the site author
can rewrite, so every DO bundle enters through a platform-owned module
(`sites/platform_guard.py`, `__paw_platform_guard.mjs`; on a draft the draft guard is
the same module, key check first). It re-exports the site's entry (DO classes stay
exported) and reads the platform vars, which the author cannot override:
`PAW_DO_SUSPENDED=1` answers 503 to every request the Worker handles and skips
`scheduled` / `queue` handlers; `PAW_DO_THROTTLED=1` answers 429 to every WebSocket
upgrade. Residuals: Durable Object alarms already scheduled inside an object keep
firing (a fetch wrapper cannot reach them); with `run_worker_first` an asset path
reaches the Worker and gets the 503 while suspended; `ROOM_MAX_PEERS`,
`ROOM_MAX_ROOMS` and `ROOM_MAX_SITE_PEERS` are still enforced by the recipe's code.

**Platform vars** (plain_text, they win over owner values), set on every deploy of a
DO bundle, drafts included:

| Var | Value |
|---|---|
| `ROOM_MAX_PEERS` | 10 on free; `PAW_SITES_DO_ROOM_MAX_PAID` on paid (default 50, clamped to 1..50). Drafts use the pocket's plan. |
| `ROOM_MAX_ROOMS` | Rooms a site may hold open: 5 on free; `PAW_SITES_DO_ROOM_MAX_ROOMS_PAID` on paid (default 20, clamped to 1..200). Drafts too. |
| `ROOM_MAX_SITE_PEERS` | Peers across all of a site's rooms: 30 on free; `PAW_SITES_DO_SITE_MAX_PEERS_PAID` on paid (default 200, clamped to 1..1000). Drafts too. |
| `PAW_DO_THROTTLED` | `"1"` while the usage sweep has the site over its daily ceiling, else `"0"`. The platform wrapper refuses WebSocket upgrades with 429. Drafts are never throttled. Pushed to the live script every sweep run. |
| `PAW_DO_SUSPENDED` | `"1"` past `PAW_SITES_DO_SUSPEND_FACTOR` x the ceiling: the platform wrapper answers 503 and skips scheduled / queue handlers. Pushed every sweep run. |
| `PAW_SITE_ORIGINS` | Comma-separated origins the recipe accepts a WebSocket from, besides the Worker's own host. Published: the public URL (workers.dev host on the account target, `https://<site id>.<PAW_CF_SITES_DOMAIN>` on dispatch) plus every `live` custom domain, all https. Draft: that build's preview origin `https://<token>.<preview base host>` (the token rotates per build; the draft redeploys per build). Never the builder origin; the preview proxy checks that. Refreshed live when a custom domain goes live or is removed. |

**Env.**

| Variable | Default | Meaning |
|---|---|---|
| `PAW_SITES_DURABLE_OBJECTS` | off | The feature flag. |
| `PAW_SITES_DO_MAX_CLASSES` | 3 (max 5) | Classes per paid site. Free sites get 1. |
| `PAW_SITES_DO_ACCOUNT_BUDGET` | 300 | On the `account` target, a NEW class is refused once the account's DO namespaces of published scripts reach this. Fails closed when the count cannot be read. |
| `PAW_SITES_DO_DRAFT_BUDGET` | 100 | The same, for `paw-draft-*` scripts only, so drafts cannot starve published sites. |
| `PAW_SITES_DO_WORKSPACE_QUOTA` | 5 | Live DO classes one workspace may hold (sites and drafts counted separately); checked when a deploy creates a class. |
| `PAW_SITES_DO_DRAFT_ROTATIONS_PER_HOUR` | 3 | Draft script rotations on a diverged history, per pocket per hour. |
| `PAW_SITES_DO_ROOM_MAX_PAID` | 50 | `ROOM_MAX_PEERS` on paid sites (1..50). |
| `PAW_SITES_DO_ROOM_MAX_ROOMS_PAID` | 20 | `ROOM_MAX_ROOMS` on paid sites (1..200). |
| `PAW_SITES_DO_SITE_MAX_PEERS_PAID` | 200 | `ROOM_MAX_SITE_PEERS` on paid sites (1..1000). |
| `PAW_SITES_DO_SUSPEND_FACTOR` | 3 | Suspend at this many times the daily ceiling. |
| `PAW_SITES_DO_METERING_FAILURES_ALERT` | 6 | Consecutive failed analytics reads before an ERROR and a red sweep monitor. |
| `PAW_SITES_DO_DAILY_REQUESTS_FREE` | 100000 | Daily DO requests before a free site is throttled; 0 disables. |
| `PAW_SITES_DO_DAILY_REQUESTS_PAID` | 3000000 | Same for paid sites. |
| `PAW_SITES_DO_METERING_MINUTES` | 60 | How often the usage sweep reads analytics. |
| `PAW_SITES_DO_TEARDOWN_MAX_ATTEMPTS` | 6 | Teardown retries before a row goes to an operator. |

**Token scopes.** On top of the Workers scripts scope: `Account Analytics: Read` for
the usage sweep (GraphQL analytics).

**Usage metering** (`sweep_do_usage`, in the cluster sweep loop). At most once per
`PAW_SITES_DO_METERING_MINUTES`, for every live site with DO classes: reads today's
requests (`durableObjectsInvocationsAdaptiveGroups.sum.requests`), active time
(`durableObjectsPeriodicGroups.sum.activeTime`) and stored bytes
(`durableObjectsStorageGroups.max.storedBytes`) per namespace from
`POST /client/v4/graphql`, maps namespaces to scripts through the namespaces list,
stores the day on `Site.do_usage` (35 days kept), sets `Site.do_throttled` while
today's requests are past the plan's ceiling and `Site.do_suspended` past
`PAW_SITES_DO_SUSPEND_FACTOR` times it. Every run pushes both current values to the
live script (not only on a change, so a publish that uploaded a stale value is fixed
within a run); they are stored only once the push worked. After
`PAW_SITES_DO_METERING_FAILURES_ALERT` failed reads in a row it logs an ERROR naming
the likely cause (the token lacks `Account Analytics: Read`) and raises, so the
`sweep:sweep_do_usage` monitor goes red; `do_metering.metering_status()` has the count
and last error (per process, reset on restart). It fails open: if the namespaces list or
the request counts cannot be read, nothing changes and the error is logged; a failed
duration or storage read only leaves those numbers at 0. The flag clears by itself on
a new day under the ceiling. To lift a throttle early, raise the ceiling; the next
sweep lifts it on the live script.

**Live var updates** (`durable_objects.set_platform_vars_live`). `PAW_DO_THROTTLED`
and `PAW_SITE_ORIGINS` change without re-uploading code, through the script settings
API: `GET` then `PATCH .../workers/scripts/<name>/settings` (the dispatch path on
WfP), the `settings` part carrying `bindings`
([docs](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/script_and_version_settings/methods/edit/)).
The PATCH lists every binding the script has: the platform vars being set as
`plain_text`, every other one as `{"type": "inherit", "name": ...}` (secrets
included), so nothing read is echoed back and nothing changed since the GET is
reverted. The settings API documents `inherit` as a type-agnostic binding with no
per-type list, so it is limited to the types our deploys create (ai, assets, d1,
durable_object_namespace, kv_namespace, plain_text, queue, r2_bucket, secret_key,
secret_text); a script with any other type is not PATCHed
(`sites.do_settings_unsupported`, logged, set on the next deploy). Deploys and live
PATCHes of one script run under one lock (`sites/do_lock.py`: an in-process lock,
plus the Redis lease `sites-do-script:<script>` when the multi-worker switch is on).
Every caller fails open: an error is logged and the site keeps serving.

**Delete.** The cascade's `do` step (before `script`) runs the teardown: a stub upload
with `{old_tag, new_tag: "paw-tombstone", deleted_classes}` (deletes every object and
its data), `DELETE ...?force=true`, then a check that the namespaces list has no row
for the script. The `script` step then deletes with `force` (a 404 is success). If the
teardown cannot finish, the ledger records `partial-needs-operator` and a
`site_do_teardowns` row is queued (the Site doc is deleted when the cascade ends).
`sweep_do_teardowns` retries it with a forced delete and the namespace check (never a
stub upload, which would recreate the deleted script), backing off from 10 minutes to
a day; after `PAW_SITES_DO_TEARDOWN_MAX_ATTEMPTS` the row's `state` becomes `operator`
and an ERROR log names the script, target, classes and last error. Draft purges use
the same teardown and the draft sweeper retries them.

**Operator runbook.**

1. *A teardown handed to an operator.* Find the ERROR log `needs an operator`, or
   query `site_do_teardowns` with `state: "operator"`. List the account's namespaces
   (`GET /accounts/{id}/workers/durable_objects/namespaces`) and look for rows whose
   `script` is the logged one. If the script still exists, delete it with
   `DELETE .../workers/scripts/<name>?force=true` (or the dispatch-namespace path for
   `target: dispatch`). Once no namespace row remains, delete the teardown row.
2. *A site is throttled.* Check `Site.do_usage` for today's numbers. Either it is real
   traffic (leave it; it lifts tomorrow, or move the site to a paid plan), or the
   ceiling is too low for the plan (raise `PAW_SITES_DO_DAILY_REQUESTS_*`). If the
   log shows `could not push PAW_DO_THROTTLED`, the stored flag did not change and
   the next sweep retries; check the token's Workers Scripts: Edit scope.
3. *Usage stays at zero or the sweep logs `usage read failed`.* Analytics lag by a
   few minutes; a persistent failure usually means the token lacks
   `Account Analytics: Read` or a GraphQL field was renamed. Run the spike's
   scenario D (below) to print the live schema.
4. *A publish is refused with `sites.do_history_diverged`.* The build's migrations do
   not start with what is applied. Restore the earlier entries unchanged and append;
   never edit or reorder applied migrations.
5. *`sites.do_account_budget` / `sites.do_budget_unknown`.* The account target is near
   its DO namespace budget, or the list could not be read. Clean up with step 1, or
   move project sites to Workers for Platforms.

**The staging spike** (`scripts/sites_do_spike.py`). Never against production, never
via wrangler. It uses our own client against a staging account:

```bash
PAW_CF_ACCOUNT_ID=<staging account id> PAW_CF_API_TOKEN=<token> \
  uv run --group ee python scripts/sites_do_spike.py
# optional: PAW_SPIKE_WRITE=1 writes a row into each DO over workers.dev first
```

The token needs Workers Scripts: Edit and Account Analytics: Read. It deploys
`paw-spike-do-<hex>` scripts with one SQLite class and prints, without secrets:
(A) whether an upload without `migrations` is accepted once a tag exists; (B) the
namespaces after a tombstone then a forced delete; (C) the namespaces after a forced
delete alone; (D) the durableObjects* GraphQL datasets and their fields, and
`do_metering.read_usage` for a spike script; (E) the live settings push: a script
with a `secret_text` binding and a plain_text var gets the var changed through
`set_platform_vars_live`, then it prints, as booleans only, whether the secret is
still listed in the settings (by name), whether the Worker still reads it
(`env.SECRET === <expected>`, compared inside the Worker), whether the var changed
in the settings and in the Worker, and whether a `durable_object_namespace` and a
`d1` binding survived (listed in the settings, and usable from the Worker). Every script is force-deleted in a `finally`.

**Scenario E must print all `True` before `PAW_SITES_DURABLE_OBJECTS` goes on.** The
throttle and origins pushes rewrite a live script's bindings; if `inherit` does not
keep secrets on this account, they would strip a site's secrets.

## Project D1 migrations

A project that binds D1 (the `d1-drizzle` recipe) ships its schema as
`migrations/*.sql`. Publish applies them to the site's database through the D1 HTTP
API (`POST /d1/database/{id}/query`), after the binding checks and before the first
upload. Wrangler, drizzle-kit and the project's own config never run on the API host.

- **Source.** Top-level `migrations/*.sql` from the pocket's source map, the same
  files the stored bundle was built from (the bundle is keyed by their content hash).
  Anything else under `migrations/`, such as drizzle's `meta/_journal.json`, is
  ignored. File names may use letters, digits, `.`, `-` and `_`.
- **Order and tracking.** Filename order. Each applied migration is a row in
  `_paw_migrations (name, applied_at, sha256)` inside the site's D1, and is skipped
  on later publishes.
- **Applied migrations are immutable.** A file whose sha256 differs from its recorded
  row refuses the publish with `422 sites.migration_changed`; add a new migration.
- **Statements** split on drizzle's `--> statement-breakpoint` and on `;`, never inside
  a string, a quoted identifier or a comment, and not inside a `CREATE TRIGGER ... END`
  body. A migration's statements and its tracking row go to D1 as one batch.
- **Destructive changes.** A pending `DROP TABLE`, `ALTER TABLE ... DROP COLUMN` or
  `DELETE` without `WHERE` whose table already holds rows refuses the publish with
  `422 sites.migration_destructive`, unless the publish carries
  `confirm_destructive_migrations: true` (`POST /sites/publish`; it is captured with
  a paid site's pending deploy too). Drizzle's table rebuild (copy rows into a new
  table, drop the old one, rename the new one into place) keeps the data and is not
  refused. A table that is empty or does not exist yet is not data.
- **Failure.** A failed migration refuses the publish with `422
  sites.migration_failed`, naming the migration and D1's error. Nothing has been
  uploaded, so the live site keeps serving the previous version.
- **Drafts** do not touch D1: a project draft with a worker previews its assets only.
- **Teardown.** The delete cascade's `d1` step deletes the database recorded in
  `Site.d1_database_id`, for project sites as for dynamic ones. The pre-delete export
  only covers tables a dynamic site declares, so a project site's own tables are not
  in it yet.

## Performance

A site Worker runs in the Cloudflare location nearest the visitor, but its D1 primary
(and an R2 bucket) lives in one region. Every query from a far location pays that
round trip. This section covers the settings we send with every bundle upload and the
ones an operator can turn on.

Every block below goes into the script-upload `metadata` on both targets. The account
[script upload](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/methods/update/)
and the
[dispatch namespace script upload](https://developers.cloudflare.com/api/resources/workers_for_platforms/subresources/dispatch/subresources/namespaces/subresources/scripts/methods/update/)
both list `placement`, `observability` and `limits` in their metadata schema (checked
2026-10-07). They are sent only for a bundle with worker modules. An assets-only
Worker never runs code (asset requests are served before the Worker), so it gets none
of them.

| Metadata | Sent when | Env |
|---|---|---|
| `observability` | always (worker bundles) | `PAW_SITES_OBSERVABILITY=0` turns it off; `PAW_SITES_OBSERVABILITY_SAMPLE` (default `0.1`) |
| `limits` | always (worker bundles), by the site's plan | `PAW_SITES_CPU_MS_FREE` (50), `PAW_SITES_CPU_MS_PAID` (300), `PAW_SITES_SUBREQUESTS_FREE` (50), `PAW_SITES_SUBREQUESTS_PAID` (10000) |
| `placement` | opt-in, and only when the worker binds D1 or R2 | `PAW_SITES_SMART_PLACEMENT=1` |

### Observability

Every worker bundle is uploaded with:

```json
"observability": {
  "enabled": true,
  "head_sampling_rate": 0.1,
  "logs": {"enabled": true, "invocation_logs": true},
  "traces": {"enabled": true, "head_sampling_rate": 0.1}
}
```

- The field names come from the upload API's metadata schema: `observability.enabled`,
  `head_sampling_rate` ("From 0 to 1 ... Default is 1"), `logs.{enabled,
  invocation_logs}` and `traces.{enabled, head_sampling_rate}`. An API upload with no
  `observability` block gets no Workers Logs, which is why sites deployed this way
  had none.
- The rate is `PAW_SITES_OBSERVABILITY_SAMPLE` (a number from 0 to 1, default `0.1`).
  It applies to both logs and traces. Anything outside 0..1 is logged and the default
  is used. `PAW_SITES_OBSERVABILITY=0` (or `false` / `no` / `off`) sends no block.
- Cost: Workers Logs includes 20 million log events a month on Paid, then $0.60 per
  million, with 7-day retention. From 2026-12-01 it moves to Cloudflare Observability
  pricing ([Workers Logs](https://developers.cloudflare.com/workers/observability/logs/workers-logs/)).
  Metrics (requests, errors, CPU and wall time) are collected whatever this setting
  is.

### Per-site CPU and subrequest limits

Every worker bundle is uploaded with `limits: {cpu_ms, subrequests}`, picked by the
site's plan. "Paid" means the same answer the binding provisioner uses for R2 and the
larger KV cap (`entitlements.site_paid_backends_entitled`: a paid site tier with an
active subscription). An unknown plan counts as free.

| Site plan | `cpu_ms` | `subrequests` |
|---|---|---|
| Free | 50 (`PAW_SITES_CPU_MS_FREE`) | 50 (`PAW_SITES_SUBREQUESTS_FREE`) |
| Paid | 300 (`PAW_SITES_CPU_MS_PAID`) | 10000 (`PAW_SITES_SUBREQUESTS_PAID`) |

- The API describes `cpu_ms` as "The amount of CPU time this Worker can use in
  milliseconds" and `subrequests` as "The number of subrequests this Worker can make
  per request" (script upload metadata schema, both targets).
- Ranges ([Wrangler `limits`](https://developers.cloudflare.com/workers/wrangler/configuration/#limits),
  [limits](https://developers.cloudflare.com/workers/platform/limits/)): `cpu_ms` up
  to 300,000; `subrequests` up to 10,000,000 on a paid account (default 10,000; 50 on
  a free account). An env value outside 0..max, or not a number, is logged and the
  plan default is used. `0` leaves that field out, so Cloudflare's account default
  applies (30 s CPU on Paid).
- The subrequest defaults mirror Cloudflare's own Free and Paid account defaults. The
  configured limit also caps calls to Cloudflare services (D1, KV, R2 bindings count
  as subrequests to internal services), so a free site gets 50 of those per request.
- A Worker that keeps going over its CPU limit is terminated with an exceeded-CPU
  error; a short burst is tolerated per isolate.

**Account target.** "Limits are only supported for the Standard Usage Model"
([Wrangler `limits`](https://developers.cloudflare.com/workers/wrangler/configuration/#limits)).
Standard is the usage model of a Workers Paid account, which the account target
already needs (the Free plan caps CPU at 10 ms per request, so 50 or 300 could not
apply there). Before this change an account-level site had no cap of its own: up to
the Paid default of 30 s CPU per request.

**Dispatch target.** The dispatch upload schema accepts the same `limits` block, so
we send it. The Workers for Platforms docs only document per-tenant limits set by the
dispatch Worker (`env.DISPATCHER.get(name, {}, {limits: {cpuMs, subRequests}})`,
[custom limits](https://developers.cloudflare.com/cloudflare-for-platforms/workers-for-platforms/configuration/custom-limits/)).
Whether an upload-time `limits` is enforced on a user Worker reached through a
dispatch binding is not documented. Our dispatch worker sets no limits today. Until a
live WfP check confirms the upload-time cap, treat the dispatch Worker as the place
to enforce it (follow-up: pass the plan's limits in `DISPATCHER.get`, for example from
script tags).

### Smart Placement (opt-in, off by default)

`PAW_SITES_SMART_PLACEMENT=1` (or `true` / `yes` / `on`) uploads a worker bundle that
binds `d1` or `r2_bucket` with `placement: {"mode": "smart"}`. Unset, or any other
value, sends no `placement`. Assets-only bundles, and workers that bind nothing
regional (only assets, KV or secrets), never get it. The next publish of each site
applies a change.

It is off by default because it rarely helps a paw site
([Placement](https://developers.cloudflare.com/workers/configuration/placement/)):

- **Quiet sites never get placed.** "Smart Placement requires consistent traffic to
  the Worker from multiple locations to make a placement decision." A low-traffic
  site stays `INSUFFICIENT_INVOCATIONS` and runs as if it were off.
- **It moves the whole script.** "The entire Worker script is placed as a single
  unit," so with `assets.run_worker_first` Cloudflare says "placement decisions are
  not optimized correctly"
  ([Worker script routing](https://developers.cloudflare.com/workers/static-assets/routing/worker-script/)).
  The `vite-react-hono` starter sets `run_worker_first: ["/api/*"]`.
- **It works against D1 read replicas.** Placement runs the Worker near the primary;
  replicas serve reads near the visitor. A site that adopts the Sessions API (below)
  should not also use placement.

When it does run: it only affects `fetch` handlers (not RPC or named entrypoints),
analysis takes up to 15 minutes after a deploy, 1% of requests stay unplaced as a
baseline, static assets are always served nearest the visitor, and D1 gets no special
treatment since 2025-02-13 ([changelog](https://developers.cloudflare.com/workers/platform/changelog/)).
The status reads back from `GET /accounts/{acct}/workers/services/{name}`
(`SUCCESS`, `INSUFFICIENT_INVOCATIONS`, `UNSUPPORTED_APPLICATION`, or absent before
analysis). We don't read it yet.

**Workers for Platforms: unverified.** The dispatch upload schema accepts
`placement`, but the placement docs never mention dispatch namespaces, and a user
Worker reached through a dispatch binding may not be placed. Don't rely on placement
on the `dispatch` target until it is checked on a live WfP namespace.

Follow-up: a per-site flag. The Site model has no settings block that fits it, so for
now placement is one operator switch for every bundle deploy.

### D1 location hint

`PAW_SITES_D1_LOCATION_HINT` sets `primary_location_hint` on every **new** site D1
(the bundle provisioner and the dynamic-site provision job both create through
`CloudflareClient.create_database`). Valid values are `wnam`, `enam`, `weur`, `eeur`,
`apac` and `oc`
([create database](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/create/),
[data location](https://developers.cloudflare.com/d1/configuration/data-location/)).
An unknown value is logged and left out.

- **The code default is unset**, so a self-hosted install is not pinned to a region
  it doesn't serve: Cloudflare then places the primary near the caller, which is the
  API host, not the visitors.
- **Our hosted deploy sets `apac`** (most of our visitors are in India).
- **Only new databases.** The hint is read when a database is created. Existing
  databases (support-desk's included) keep their primary; moving one means exporting
  it into a new database, or adding read replicas plus the Sessions API.
- A hint is a preference: "Providing a location hint does not guarantee that D1 runs
  in your preferred location." South America, Africa and the Middle East have no hint.

Follow-up: a per-site hint (from the owner's audience). The Site model has no
obvious field for it; `create_database` would take the hint as an argument and the
two callers would pass the site's value before the env default.

### D1 read replication (opt-in)

`PAW_SITES_D1_READ_REPLICATION=1` creates new site databases with
`read_replication: {"mode": "auto"}`. Replicas cost nothing extra, but they only serve
queries a Worker sends through the Sessions API (`env.DB.withSession(...)`); every
other query, and every REST API query (our migrations), still goes to the primary.
Replicas are asynchronous, so a template that adopts sessions must pass bookmarks to
read its own writes
([read replication](https://developers.cloudflare.com/d1/best-practices/read-replication/)).
It is off by default until a template uses sessions. An existing database is switched
with `PUT /accounts/{acct}/d1/database/{id}` and `{"read_replication": {"mode":
"auto"}}`; we have no code that does that yet.

### Asset caching

`_headers` from the build (`assetsConfig._headers`, or lifted from `assetsDir`) is sent
as `assets.config._headers`, so a template that ships
`/_next/static/*  Cache-Control: public,max-age=31536000,immutable` gets immutable
caching for its hashed files. It applies to responses served by the asset layer, not
to responses the Worker generates. This forwarding is not new; what was missing is a
`_headers` file in the starters, which paw-sites adds.

`html_handling` defaults to `auto-trailing-slash`: `about.html` is served at `/about`,
`about/index.html` at `/about/`, and the other spelling gets a 307 to the canonical one
([HTML handling](https://developers.cloudflare.com/workers/static-assets/routing/advanced/html-handling/)).
A template whose links do not match its output format (`/about` links to a
`about/index.html` build) pays an extra redirect on every such click; fix that in the
template, not by changing the default. `not_found_handling` defaults to `none`, so an
unmatched path goes to the Worker with no extra hop.

### Cold starts

There is no platform setting for cold starts. Cloudflare already pre-warms a Worker
during the TLS handshake and routes to instances that are already loaded
([Eliminating cold starts 2](https://blog.cloudflare.com/eliminating-cold-starts-2-shard-and-conquer/)).
What a cold start costs is the script size and the work in its global scope
([startup limit](https://developers.cloudflare.com/workers/platform/limits/)), so the
levers live in the templates: smaller worker bundles, no top-level initialization, and
prerendered pages served as static assets instead of through the Worker.

Measure before blaming a cold start: split curl's `time_connect` / `time_appconnect`
from TTFB and read the Worker's own `Server-Timing`. Client-side TCP retransmits (a
lost SYN waits the 1 s initial RTO) look exactly like a slow cold start.

## Secrets

Values come from the per-site secret store (`ee/pocketpaw_ee/sites/site_secrets.py`,
API in `docs/api-reference.md` under "Sites — Secrets"). The agent asks for a secret
by name with `request_site_secret`; the pocket owner sets the value in the builder.
Nothing about a value is ever in the source map, `paw-build.json`, the build artifact
or the sandbox: the build only names secrets.

- **Every set secret binds**, requested or not, as
  `{"type": "secret_text", "name": NAME, "text": value}` in the upload metadata. The
  Worker reads it as `env.NAME`. `sites.service.deploy_bundle`'s provision step loads
  them (`site_secrets.secrets_for_deploy`, the only code that decrypts) after the KV /
  R2 provisioning and before the first upload.
- **Required secrets.** A `secret` binding request with `required: true` (what
  paw-sites' project build emits for a recipe's required secrets) or a name in
  `requiredSecrets` that is not set refuses the deploy before any upload with 422
  `sites.secrets_missing`: "This site needs secrets that are not set: X, Y. Set them in
  the builder (Secrets) and publish again." An optional `secret` request that is unset
  is skipped with a warning. Recipes declare their secrets in `recipe.json`
  (`paw.recipes.json` in paw-sites); pocketpaw does not read recipes, so the build
  passes the requirement through `bindingRequests` / `requiredSecrets`.
- **Name clashes.** A set secret whose name matches another binding (`DB` as both a D1
  and a secret) refuses the deploy; rename one.
- **Redaction.** Warnings, refusals and log lines name secrets, never values.
  `ProvisionedResources.secrets` and `PawBundle.bindings` are excluded from `repr`.
  A test (`tests/ee/sites/test_site_secrets.py`) deploys through the real client and
  asserts the value occurs exactly once across every request body, inside the
  `secret_text` binding, and never in the captured logs.
- **Teardown.** The delete cascade's `records` step removes the site's secrets and
  pending requests, best effort: a failure is logged and the cascade goes on.
- **Key.** Values are Fernet-encrypted with `CLOUD_ENCRYPTION_KEY`. Rotating that key
  makes stored values undecryptable; a publish then fails with
  `cloud.value_undecryptable` until the owner re-enters them.

## Before enabling: live smoke test

Everything above is tested against a mocked Cloudflare API that replays the spike's
captured requests. It has **not** been run against a real dispatch namespace. Before
the `project` engine or any template publishes through this path, someone with
credentials has to:

1. Upload the spike's Next bundle (`worker.js`, about 4.6 MiB, plus its assets) to a
   staging dispatch namespace with `deploy_bundle`.
2. Hit it through the staging dispatch worker: `/`, a route handler, a server action,
   a static page and a `/_next/static/*` asset.
3. Confirm WfP accepts the size (its limits page doesn't state one), the startup time
   is under the 1 s limit, `_headers` applies, and an assets-only upload (no modules)
   is accepted.
4. Deploy a bundle requesting one `kv` and one `r2` binding on a paid staging site.
   Confirm the worker reads and writes both, a redeploy creates nothing new, and a
   site delete removes the namespace and bucket. Put an object in the bucket first
   once, to check that Cloudflare answers the non-empty delete with a 409 (the code
   assumes it does).

Needs `PAW_CF_ACCOUNT_ID`, `PAW_CF_API_TOKEN` (Workers Scripts edit) and a staging
namespace. See `docs/runbooks/2026-07-09-dynamic-sites-real-cf-smoke.md` for the
existing smoke-test setup.
