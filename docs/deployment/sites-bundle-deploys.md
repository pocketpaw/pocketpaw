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
- **Account limits.** Script count and per-account CPU / subrequest limits apply to
  all sites together.
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
     assets: {jwt, config}}`, plus `placement: {mode: "smart"}` when the worker binds
     D1 or R2 (see "Performance" below)
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
| `do`, `queues`, `ai` | none | Refused: "not supported on Paw Sites yet". | |

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
round trip. Three settings narrow the gap.

### Smart Placement

A bundle with worker modules that binds a regional backend (`d1` or `r2_bucket` in the
mapped bindings) is uploaded with `placement: {"mode": "smart"}` in the script
metadata, on both the `account` and `dispatch` targets. Both upload APIs accept it:
[script upload](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/methods/update/),
[dispatch namespace script upload](https://developers.cloudflare.com/api/resources/workers_for_platforms/subresources/dispatch/subresources/namespaces/subresources/scripts/methods/update/),
[metadata reference](https://developers.cloudflare.com/workers/configuration/multipart-upload-metadata/).

What it does ([Placement](https://developers.cloudflare.com/workers/configuration/placement/)):

- Cloudflare measures request duration in different locations and forwards a request
  to a location that is significantly faster, usually one near the backend. 1% of
  requests stay unplaced as a baseline.
- It only affects `fetch` handlers, and only after analysis (up to 15 minutes after
  a deploy) and enough traffic from several locations. A quiet site reports
  `INSUFFICIENT_INVOCATIONS` and runs as before.
- Static assets are always served from the location nearest the visitor; assets the
  Worker fetches through its `ASSETS` binding come from where the Worker runs.
- D1 gets no special treatment: since 2025-02-13 Workers bound to D1 follow the same
  latency-based logic as every other Worker
  ([changelog](https://developers.cloudflare.com/workers/platform/changelog/)).

Assets-only bundles and workers that bind nothing regional (only assets, KV or
secrets) get no placement. KV is cached at the edge, so it does not earn placement
on its own. Set `PAW_SITES_SMART_PLACEMENT=0` (or `false` / `no` / `off`) to turn it
off for every bundle deploy; the next publish of each site drops it.

### D1 location hint

`PAW_SITES_D1_LOCATION_HINT` sets `primary_location_hint` on every **new** site D1
(the bundle provisioner and the dynamic-site provision job both create through
`CloudflareClient.create_database`). Valid values are `wnam`, `enam`, `weur`, `eeur`,
`apac` and `oc`
([create database](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/create/),
[data location](https://developers.cloudflare.com/d1/configuration/data-location/)).
An unknown value is logged and left out. Unset means Cloudflare places the primary
near the caller, which is the API host, not the visitors.

- The hint only applies when a database is created. Existing databases keep their
  primary; moving one means exporting it into a new database.
- A hint is a preference: "Providing a location hint does not guarantee that D1 runs
  in your preferred location." South America, Africa and the Middle East have no hint.
- Pick the region most visitors are in (`apac` for an India-heavy user base).

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
to responses the Worker generates.

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
