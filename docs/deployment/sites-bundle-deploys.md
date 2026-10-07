<!-- How a paw-build.json build (project engine / base app templates) is deployed
     into the Workers for Platforms dispatch namespace, and what gates enabling it. -->
# Sites: bundle deploys (`paw-build.json`)

Builds from the `project` engine and the base app templates (Next.js via OpenNext,
TanStack Start, Astro, ...) are not single `index.mjs` workers. They are a worker made
of one or more ES modules plus a directory of static assets. The sandbox build
describes that output in `paw-build.json`, and the API host deploys it into the
`paw-sites` dispatch namespace through the Cloudflare HTTP API.

Code: `ee/pocketpaw_ee/sites/bundle_deploy.py` (vetting and mapping) and
`CloudflareClient.upload_assets` / `put_worker(modules=...)` in
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
  `kv`, `r2`, `queues` and `ai` map to resources we provisioned for the site; today
  that is the site's D1 only, so every other backend request is refused with a "not
  provisioned" error. `do` is always refused for now, since it needs a class and
  migrations we don't provision. `secret` requests take values from our store; a
  missing required secret refuses the deploy. `service`, `dispatch_namespaces`,
  `tail_consumers`, `images`, routes and unknown types are never forwarded. Author ids
  are never read.
- **Limits:** modules over 64 MiB uncompressed in total
  ([Workers limits](https://developers.cloudflare.com/workers/platform/limits/)).
  Cloudflare documents no module-count limit, so we cap at 1,000 ourselves. Assets:
  100,000 files, 25 MiB per file.

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

Needs `PAW_CF_ACCOUNT_ID`, `PAW_CF_API_TOKEN` (Workers Scripts edit) and a staging
namespace. See `docs/runbooks/2026-07-09-dynamic-sites-real-cf-smoke.md` for the
existing smoke-test setup.
