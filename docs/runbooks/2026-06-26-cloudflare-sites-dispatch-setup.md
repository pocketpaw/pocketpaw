<!--
2026-06-26-cloudflare-sites-dispatch-setup.md
Created 2026-06-25 (feat/sites-cf-dispatch-worker): end-to-end runbook to take Paw
Sites live on Cloudflare Workers for Platforms — provision the account/token/zone,
create the dispatch namespace, deploy the dispatch worker, set up wildcard DNS +
TLS, set the backend env, and smoke a publish.
-->

# Runbook — Cloudflare Paw Sites dispatch go-live

Take Paw Sites from "publish uploads a worker that nobody can reach" to "a
published site is live at `https://<site_id>.<PAW_CF_SITES_DOMAIN>`."

This is the **serving layer**. Publishing already uploads each generated site as a
**user worker** into the `paw-sites` Workers-for-Platforms (WfP) **dispatch
namespace** (`cloudflare_client.put_worker`). But a user worker in a dispatch
namespace is **not directly URL-addressable** — it only serves when a **dispatch
worker** routes a request to it. This runbook stands up that dispatch worker and
wires the URL.

> Cross-reference: the workspace-level go-live draft
> `docs/design/drafts/2026-06-20-cloudflare-deploy-activation.md` (in the
> paw-workspace repo) covers activating the Cloudflare account + token + zone at the
> deploy layer. This runbook is the sites-specific serving half and assumes that
> account/token/zone work is either done here (steps 1–2) or already done there.

## Prerequisites

- A Cloudflare account on a **Workers Paid** plan (WfP dispatch namespaces require
  it).
- A zone in that account you can add DNS + certificates to (e.g. `interacly.com`).
- `wrangler` installed via the dispatch worker's `package.json`
  (`ee/pocketpaw_ee/sites/cloudflare/dispatch-worker`) — pinned to an aged version
  for the supply-chain gate; do not bump to latest.
- Backend deploy access (Coolify) to set env vars.

Pick your **sites domain** now and use it consistently below. Two valid shapes:

- **First-level** (simplest TLS): `PAW_CF_SITES_DOMAIN = sites-domain.com` where
  that is a CF zone, so sites serve at `https://<id>.sites-domain.com` and Universal
  SSL's `*.<zone>` cert covers them with no extra cert.
- **Sub-subdomain**: `PAW_CF_SITES_DOMAIN = sites.hzd.interacly.com`, so sites serve
  at `https://<id>.sites.hzd.interacly.com`. This is a **2-level-deep wildcard**
  (`*.sites.<zone>`) and needs Total TLS or an Advanced Certificate (step 5b).

The examples below use `sites.hzd.interacly.com` in the `interacly.com` zone —
substitute your own.

## 1. Provision account, token, zone

1. Note the **Account ID** (Cloudflare dashboard → any domain → right sidebar).
2. Create an **API token** with permissions:
   - `Account → Workers Scripts → Edit` (deploy workers, manage dispatch
     namespaces / user-worker uploads),
   - `Zone → DNS → Edit` and `Zone → SSL and Certificates → Edit` for the sites
     zone (custom hostnames + certs),
   - scoped to the specific account + zone.
3. Note the **Zone ID** of the sites zone.

These become `PAW_CF_ACCOUNT_ID`, `PAW_CF_API_TOKEN`, `PAW_CF_ZONE_ID`.

## 2. Create the dispatch namespace

The backend uploads each site into the namespace named by
`PAW_CF_DISPATCH_NAMESPACE` (default `paw-sites`). Create it once (idempotent):

```bash
cd ee/pocketpaw_ee/sites/cloudflare/dispatch-worker
npm install
npx wrangler dispatch-namespace create paw-sites
npx wrangler dispatch-namespace list   # confirm paw-sites is listed
```

> The namespace name MUST match `PAW_CF_DISPATCH_NAMESPACE`. If you override that
> env var, create + bind the namespace under the same name (also update the
> `namespace` in `wrangler.jsonc`).

## 3. Deploy the dispatch worker

From the same directory. Set the route to the wildcard for your sites domain —
either edit `wrangler.jsonc`:

```jsonc
"routes": [
  { "pattern": "*.sites.hzd.interacly.com/*", "zone_name": "interacly.com" }
]
```

…or pass it on deploy:

```bash
npx wrangler deploy --route '*.sites.hzd.interacly.com/*'
```

The `*.<domain>/*` wildcard route is the recommended WfP pattern — it works
regardless of DNS proxy settings and has no per-route limit.

## 4. Wildcard DNS

Add a **wildcard DNS record** for the sites domain, **proxied (orange cloud)**, in
the zone:

```
Type: AAAA   Name: *.sites.hzd.interacly.com   Content: 100::   Proxy: ON
```

(A placeholder target like `AAAA 100::` or `A 192.0.2.1` is fine — the worker
route, not the origin, serves the request. The record only needs to exist and be
proxied so the edge runs the worker.)

## 5. TLS

A published site is served over HTTPS, so the wildcard host must have a matching
cert.

- **5a. First-level domain** (`*.<zone>`): covered by **Universal SSL**
  automatically. Nothing to do.
- **5b. Sub-subdomain** (`*.sites.<zone>`, 2 levels deep): **NOT** covered by
  Universal SSL. Either:
  - enable **Total TLS** on the zone (SSL/TLS → Edge Certificates → Total TLS), or
  - add an **Advanced Certificate** that includes `*.sites.<zone>`.

  Without one of these, browsers get a TLS error even though the worker is live.

## 6. Backend env

Set these in the backend env (Coolify) so publish takes the real CF path AND
stamps the public URL:

```
PAW_CF_ACCOUNT_ID=<account id>
PAW_CF_API_TOKEN=<api token>
PAW_CF_ZONE_ID=<zone id>
PAW_CF_DISPATCH_NAMESPACE=paw-sites           # must match step 2
PAW_CF_SITES_DOMAIN=sites.hzd.interacly.com   # the domain you routed + DNS'd
```

> `PAW_CF_SITES_DOMAIN` must ALSO be added to the canonical Coolify `.env.example`
> in the paw-workspace repo (`deploy/coolify/`). The in-repo
> `.env.enterprise.example` is updated alongside this change, but the deploy
> template lives in the workspace repo and is out of scope for this repo's PR.

Restart the backend so the new env is read.

## 7. Smoke

```bash
# Route + worker live (no published site needed):
curl -s https://anything.sites.hzd.interacly.com/__paw_health
# expect: ok

# Publish a site through the app, then hit its subdomain:
curl -sI https://<24-hex-site-id>.sites.hzd.interacly.com/
# expect: 200 + the rendered site HTML
```

The published Site doc's `url` should now read
`https://<site_id>.sites.hzd.interacly.com`. Confirm in the dashboard (the site
opens) or via the API (`GET /sites`).

### Reading the codes

| Code | Meaning |
|------|---------|
| `200 ok` on `/__paw_health` | route + dispatch worker are live |
| `404 Not found` | label is not a 24-hex site id (probe / apex) |
| `404 Site not found` | valid id, but no user worker in the namespace (unpublished / deleted) |
| `502 Site error` | the user worker threw at runtime |
| TLS error | step 5 cert does not cover the wildcard — fix the cert |

## Custom domains

**Updated 2026-08-12.** Custom domains now work — but only in `workers` deploy mode
(`PAW_CF_DEPLOY_MODE=workers`), not on the dispatch-worker path this runbook sets up.

In `workers` mode each site is deployed as its own Worker (`paw-site-<site_id>`), so
connecting a domain writes a Cloudflare Worker **route** scoped to that one hostname
(`<hostname>/*` → that site's Worker) alongside the custom hostname. Two API calls
from the control plane, no dispatcher, and nothing to do in the dashboard per site.

### One-time setup, required before the first domain connects

1. **Enable Cloudflare for SaaS** on the zone named by `PAW_CF_ZONE_ID`.
2. **Create a proxied fallback-origin record** on that zone. Cloudflare's own example
   of an originless record is `service.example.com AAAA 100::`; it must be proxied.
3. **Set it as the fallback origin**:
   `PUT /zones/{zone}/custom_hostnames/fallback_origin {"origin": "<that hostname>"}`.
4. **Set `PAW_CF_CNAME_TARGET`** to the hostname customers should paste at their own
   registrar — normally the same record. There is no default, and connecting a domain
   **fails closed** without it: the value was previously derived as
   `<zone_id>.cdn.cloudflare.net`, which resolves to nothing, so every customer who
   pasted it waited forever on a hostname that could never validate.
5. **The API token needs `Zone → Workers Routes: Edit`** in addition to SSL and
   Certificates, or the route call 403s after the hostname is created (the add rolls
   the hostname back and reports the Cloudflare error).

### Still a follow-up — custom domains on the WfP dispatch path

Unchanged for `wfp` mode, which is what this runbook configures. The inbound
`hostname` is no longer `<site_id>.<domain>`, so the dispatch worker's leftmost-label
trick 404s it — and in practice the request never reaches the dispatch worker at all,
because its route is `*.<PAW_CF_SITES_DOMAIN>/*` and nothing matches `example.com/*`;
it falls through to the fallback origin. Connecting a domain to a wfp-deployed site
therefore still does not serve that site. It needs a `hostname → site_id` map in the
dispatch worker (a routing KV populated at domain-connect time, or a backend lookup).
The control plane deliberately writes **no** route for a wfp site rather than one
naming a script Cloudflare cannot find.
