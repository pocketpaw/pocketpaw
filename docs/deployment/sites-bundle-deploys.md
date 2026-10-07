<!-- How a paw-build.json build (project engine / base app templates) is deployed
     into the Workers for Platforms dispatch namespace, and what gates enabling it. -->
# Sites: bundle deploys (`paw-build.json`)

Builds from the `project` engine and the base app templates (Next.js via OpenNext,
TanStack Start, Astro, ...) are not single `index.mjs` workers. They are a worker made
of one or more ES modules plus a directory of static assets. The sandbox build
describes that output in `paw-build.json`, and the API host deploys it into the
`paw-sites` dispatch namespace through the Cloudflare HTTP API.

Code: `ee/pocketpaw_ee/sites/bundle_deploy.py` (vetting and mapping),
`ee/pocketpaw_ee/sites/binding_provisioner.py` (per-site KV namespaces and R2
buckets) and `CloudflareClient.upload_assets` / `put_worker(modules=...)` in
`ee/pocketpaw_ee/sites/cloudflare_client.py` (the wire calls).

## When it runs

- In the WfP publish path, a build whose project dir contains `paw-build.json` takes the
  bundle deploy. No existing engine (ripple, svelte, react, html) writes that file, so
  their deploys are unchanged: a single-module PUT, or the one-module multipart with a
  D1 binding for dynamic sites.
- `sites.service.deploy_bundle(site, build_dir)` is the service entry point for the
  `project` engine. That engine's publish path is not wired yet; this is the call it
  will make.
- The `workers` and `local` deploy modes don't take bundles.

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

## Upload sequence

The same three calls wrangler makes for a WfP deploy (captured in the
2026-10-07 next-on-wfp spike):

1. `POST /accounts/{acct}/workers/dispatch/namespaces/{ns}/scripts/{site_id}/assets-upload-session`
   with `{"manifest": {"/path": {"hash": <32 hex>, "size": n}}}`. Cloudflare returns a
   session JWT and the `buckets` of hashes it still needs.
2. `POST /accounts/{acct}/workers/assets/upload?base64=true`, one call per bucket,
   `Authorization: Bearer <session jwt>` (not the account token), multipart with one
   base64 part per hash. The last response carries the completion JWT. If there are
   no buckets, the session JWT is the completion JWT.
3. `PUT /accounts/{acct}/workers/dispatch/namespaces/{ns}/scripts/{site_id}`, multipart:
   - `metadata`: `{main_module, bindings, compatibility_date, compatibility_flags,
     assets: {jwt, config}}`
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
| `d1` | `{type: "d1", name, id}` | The site's own D1 (`Site.d1_database_id`, created by the dynamic-site provision job). One per site. | Free and up |
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
An unrecorded one is looked up by its derived name first (a namespace by title in
the namespace list, a bucket with `GET /r2/buckets/{name}`), so a create that died
before the save is found rather than duplicated. The doc is saved after each create.
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
`Workers R2 Storage Write` on `PAW_CF_API_TOKEN`, in addition to the Workers scripts
scope the deploy already uses.

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
