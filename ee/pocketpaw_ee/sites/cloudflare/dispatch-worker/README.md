<!--
README.md — Paw Sites Workers-for-Platforms dispatch worker.
Created 2026-06-25 (feat/sites-cf-dispatch-worker): deploy steps for the dynamic
dispatch worker that makes published Paw Sites reachable.
-->

# Paw Sites — dispatch worker

The serving half of Paw Sites on Cloudflare Workers for Platforms (WfP).

## Why this exists

Publishing a site uploads the generated site as a **user worker** into the
`paw-sites` **dispatch namespace** (`cloudflare_client.put_worker`, called from
`sites/service.py`). In Workers for Platforms, a user worker in a dispatch
namespace is **not directly URL-addressable** — it only runs when a separate
**Dynamic Dispatch Worker** routes a request to it via `env.DISPATCHER.get(name)`.
This directory is that dispatch worker. Without it, every published site is
unreachable (and `service.py` leaves the site `url=""`).

## Routing model (v1 — subdomain per site)

- A published site serves at `https://<site_id>.<PAW_CF_SITES_DOMAIN>`
  (e.g. `https://<id>.sites.hzd.interacly.com`).
- This worker runs on the route `*.<PAW_CF_SITES_DOMAIN>/*`, takes the **leftmost
  hostname label** as the `site_id`, and dispatches
  `env.DISPATCHER.get(siteId).fetch(request)`.
- `site_id` is a Mongo ObjectId — 24 hex chars, a valid DNS label. The worker
  validates the label against `^[a-f0-9]{24}$` and 404s anything else.
- `GET /__paw_health` (and an apex / `www` host) returns `200 ok`, so the route
  can be smoke-tested before any site exists.

## Deploy

All commands run from this directory
(`ee/pocketpaw_ee/sites/cloudflare/dispatch-worker/`).

> Substitute your real `PAW_CF_SITES_DOMAIN` and zone everywhere
> `sites.hzd.interacly.com` / `interacly.com` appear below.

1. **Install** (pins an aged wrangler — see the supply-chain note below):

   ```bash
   npm install
   ```

2. **Create the dispatch namespace** (idempotent — skip if it already exists; a
   re-run errors harmlessly):

   ```bash
   npx wrangler dispatch-namespace create paw-sites
   # verify:
   npx wrangler dispatch-namespace list
   ```

   This MUST be the same namespace the backend uploads sites into
   (`PAW_CF_DISPATCH_NAMESPACE`, default `paw-sites`).

3. **Set the route.** Either add a `routes` block to `wrangler.jsonc`:

   ```jsonc
   "routes": [
     { "pattern": "*.sites.hzd.interacly.com/*", "zone_name": "interacly.com" }
   ]
   ```

   …or pass it on the deploy:

   ```bash
   npx wrangler deploy --route '*.sites.hzd.interacly.com/*'
   ```

   The `*.<domain>/*` wildcard route is the recommended WfP pattern — it works
   regardless of DNS proxy settings (orange-to-orange) and has no per-route limit.

4. **Deploy:**

   ```bash
   npx wrangler deploy
   # or: npm run deploy
   ```

5. **DNS — add a wildcard record** for the sites domain, **proxied (orange
   cloud)**, in the zone:

   ```
   *.sites.hzd.interacly.com   →   (proxied; AAAA 100:: or A 192.0.2.1 placeholder)
   ```

   The worker route, not the DNS target, serves the request; the record just has
   to exist and be proxied so the edge runs the worker.

6. **TLS — important.** A **2-level-deep** wildcard like `*.sites.<zone>` is **NOT
   covered by Universal SSL**, which only issues for `<zone>` and the first-level
   `*.<zone>`. So the default cert will not match `*.sites.<zone>`. Pick one:

   - **(a)** Make `PAW_CF_SITES_DOMAIN` a **first-level** subdomain of a CF zone
     (i.e. use `*.<zone>` directly, so the site id is the first label under the
     apex). This is covered by Universal SSL with no extra cert. **Simplest.**
   - **(b)** Keep the deeper `*.sites.<zone>` wildcard and enable **Total TLS**
     for the zone, or add an **Advanced Certificate** that covers
     `*.sites.<zone>`. Without one of these, browsers get a TLS error even though
     the worker is deployed.

7. **Backend env.** Set `PAW_CF_SITES_DOMAIN` in the backend env (Coolify) to the
   same domain you routed and DNS'd (e.g. `sites.hzd.interacly.com`). On the next
   publish, `service.py` stamps the site `url` as
   `https://<site_id>.<PAW_CF_SITES_DOMAIN>`. If it is unset, publish still works
   but the site has no public URL (a warning is logged).

## Smoke test

```bash
# the route + worker are live (no site needed):
curl -s https://anything.sites.hzd.interacly.com/__paw_health   # -> ok

# a published site (24-hex id) returns its rendered HTML:
curl -sI https://<24-hex-site-id>.sites.hzd.interacly.com/
```

A 404 means the route matched but no user worker exists for that id (unpublished
/ deleted). A 502 means the user worker threw at runtime.

## Follow-up — custom-domain routing (NOT in v1)

The leftmost-label trick only resolves the `*.<PAW_CF_SITES_DOMAIN>` subdomains.
When a site connects its **own custom hostname** (via
`cloudflare_client.create_custom_hostname` / Cloudflare for SaaS), the inbound
`hostname` is no longer `<site_id>.<domain>`, so this worker would 404 it. Custom
domains need the dispatch worker to map `hostname → site_id` — either a **routing
KV** (`env.ROUTING_KV.get(hostname)`) populated at domain-connect time, or a
backend lookup. v1 is **subdomain routing only**; the custom-hostname path is
tracked as a follow-up.

## Supply-chain note

`wrangler` is pinned to `4.100.0` (not latest) so the version is past the
workspace's 7-day minimum-release-age supply-chain gate. Bump it only to a version
that has aged past the same threshold; do not run `npm install wrangler@latest`.
