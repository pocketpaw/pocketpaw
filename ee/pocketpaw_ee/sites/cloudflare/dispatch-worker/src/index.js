// ee/pocketpaw_ee/sites/cloudflare/dispatch-worker/src/index.js
//
// Created: 2026-06-25 (feat/sites-cf-dispatch-worker) — the Workers-for-Platforms
// Dynamic Dispatch Worker for Paw Sites.
//
// WHY THIS EXISTS: publishing a Paw Site uploads the generated site as a USER
// WORKER into the `paw-sites` WfP dispatch namespace (see
// cloudflare_client.put_worker). In Workers for Platforms, user workers in a
// dispatch namespace are NOT directly URL-addressable — they only run when a
// separate dispatch worker routes a request to them via env.DISPATCHER.get(name).
// This worker is that router. It runs on the route `*.<PAW_CF_SITES_DOMAIN>/*`,
// reads the leftmost hostname label as the site id, and dispatches the request to
// the matching user worker.
//
// ROUTING (subdomain-per-site, v1): a published site is served at
// `https://<site_id>.<PAW_CF_SITES_DOMAIN>`. The site_id is a Mongo ObjectId (24
// hex chars), which is a valid single DNS label. Custom-domain routing (a site
// connecting its own hostname via Cloudflare-for-SaaS) is a FOLLOW-UP — the
// leftmost-label trick only resolves the platform subdomains; a custom hostname
// needs a hostname→site_id map (routing KV or a backend lookup). See README.md.
//
// SECURITY: these are AI-generated sites, so the namespace stays in UNTRUSTED mode
// (the WfP default). This worker enables NO trusted-mode features and reads no
// `request.cf`. DISPATCHER.get(name) throws synchronously when the script does not
// exist in the namespace, so the get + the dispatched fetch are each wrapped to
// fail closed (404 for an unknown site, 502 for a runtime error) rather than
// surfacing a raw exception.

// A site id is a Mongo ObjectId: exactly 24 hex characters. Validating the label
// against this shape means a stray/probe subdomain never reaches DISPATCHER.get.
const SITE_ID_RE = /^[a-f0-9]{24}$/i;

export default {
  async fetch(request, env, _ctx) {
    const url = new URL(request.url);
    const host = url.hostname;
    const siteId = host.split(".")[0];

    // Health affordance: a route smoke test hits `/__paw_health` (or the apex /
    // `www` host with no site label) and gets a plain 200 so the wildcard route +
    // worker can be verified live before any site exists.
    if (url.pathname === "/__paw_health" || siteId === "" || siteId === "www") {
      return new Response("ok", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    // Reject anything that is not a site id BEFORE touching the dispatcher — a
    // bare apex hit, a probe subdomain, or a malformed label is a 404, not a
    // dispatch attempt.
    if (!SITE_ID_RE.test(siteId)) {
      return new Response("Not found", { status: 404 });
    }

    // DISPATCHER.get throws synchronously when the script is not in the namespace
    // (an unpublished / deleted site) — treat that as a 404.
    let userWorker;
    try {
      userWorker = env.DISPATCHER.get(siteId);
    } catch (_e) {
      return new Response("Site not found", { status: 404 });
    }

    // The user worker runs untrusted; a throw here is a runtime failure inside the
    // generated site, so fail closed with a 502 rather than leaking the exception.
    try {
      return await userWorker.fetch(request);
    } catch (_e) {
      return new Response("Site error", { status: 502 });
    }
  },
};
