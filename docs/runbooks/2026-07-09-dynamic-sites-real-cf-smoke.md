<!-- Runbook: real-Cloudflare end-to-end smoke for Dynamic Paw Sites (Phase 0).
     Created 2026-07-09 (DP0-6). This is the moment-of-truth proof — provision a
     real per-tenant D1, migrate it, deploy, and confirm live read+write. It needs a
     real Cloudflare account + a runnable wrangler, so it is NOT part of CI. -->

# Runbook — Dynamic Paw Sites: real-Cloudflare end-to-end smoke

**Goal:** publish one no-auth guestbook dynamic site and prove the whole runtime chain
works against a real Cloudflare account — a per-tenant D1 is *created*, the migration is
*applied*, the Worker *deploys*, and the live URL both *lists* entries (read) and *accepts*
a new entry (write).

This is the binary success metric for Phase 0. The code chain is merged to `dev`
(#1670 create_database + provision_status, #1675 provision_site job, #1677 publish split,
#1681 baked wrangler); this runbook is what turns "code-complete" into "proven".

---

## 1. Operational-readiness checklist (all must be true)

A dynamic publish will silently sit in `provisioning` or fail unless every row holds:

| # | Requirement | How to satisfy / check |
|---|-------------|------------------------|
| 1 | **Deploy mode = `wfp`** | `PAW_CF_DEPLOY_MODE=wfp`. Free `workers` mode rejects dynamic (`sites.workers_dynamic_unsupported`). |
| 2 | **Workers-for-Platforms dispatch namespace + dispatch worker** | Follow `docs/runbooks/2026-06-26-cloudflare-sites-dispatch-setup.md`. `PAW_CF_DISPATCH_NAMESPACE` set (default `paw-sites`). |
| 3 | **HTTP-API creds** (create D1 + deploy Worker) | `PAW_CF_ACCOUNT_ID`, `PAW_CF_API_TOKEN`. Token needs **D1:Edit** (create databases) + **Workers Scripts:Edit** + the dispatch-namespace perms. |
| 4 | **wrangler creds** (migrate subprocess) | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (the standard wrangler env names; the same D1-capable token is fine). Set both pairs. |
| 5 | **wrangler on the box** | The enterprise image bakes it (#1681) and sets `PAW_CF_WRANGLER_CMD=wrangler`. On a non-baked host, install wrangler and point `PAW_CF_WRANGLER_CMD` at it. Verify: `wrangler --version` runs. |
| 6 | **Generator toolchain** | Already baked: `paw-sites-gen`, `bun`, the `@ripple-ui/svelte` tarball (`PAW_SITES_RIPPLE_DEP`). Verify: `paw-sites-gen --help` runs. |
| 7 | **arq worker running** | The `provision_site` job runs on the shared chat/jobs arq worker (`arq pocketpaw_ee.cloud.chat.runs.worker.WorkerSettings`). If it isn't running, the site enqueues and never provisions. Needs `POCKETPAW_REDIS_URL`. |
| 8 | **Sites domain (for a live URL)** | `PAW_CF_SITES_DOMAIN` so the provisioned site resolves at `https://<site_id>.<domain>`. |

> Quick pre-flight on the box:
> ```
> wrangler --version && paw-sites-gen --help >/dev/null && echo "toolchain OK"
> env | grep -E 'PAW_CF_|CLOUDFLARE_|POCKETPAW_REDIS_URL|PAW_SITES_' | sort
> ```

---

## 2. Build the guestbook dynamic site

Author a minimal no-auth guestbook (one object, one read source, one insert action). Either
via the `pocketpaw-create-dynamic-site` skill in chat ("a public guestbook people can sign"),
or directly with a rippleSpec carrying:

```jsonc
{
  "ui": { /* a list bound to {guestbook} + the sign form is auto-rendered */ },
  "objects": [{ "name": "entries",
    "fields": { "id": "text", "visitor": "text", "message": "text", "created_at": "timestamp" },
    "primaryKey": "id" }],
  "sources": [{ "name": "guestbook", "kind": "data", "object": "entries",
    "orderBy": "created_at", "refresh": "pocket_open" }],
  "actions": [{ "name": "sign", "object": "entries", "op": "insert" }]
}
```

Confirm the pocket is stamped `type="site"`, `pattern="dynamic"`.

## 3. Publish and watch the provision job

Publish the site (chat "publish this", or the publish endpoint). A **dynamic** publish returns
immediately with `{ "code": "provisioning", "job_id": ... }` — it does NOT block. Then watch:

- **Job status:** `GET /workspaces/{workspace_id}/jobs/{job_id}` → `queued → running → done | failed`.
- **Site status:** the Site doc's `provision_status`: `none → provisioning → provisioned` (or `failed`).

On success the Site flips to `provisioned` + `deployed=True` with a live `url`.

## 4. Verify the runtime chain (the actual proof)

1. **D1 exists** — in the Cloudflare dashboard (or `wrangler d1 list`), a database named
   `paw-site-<site_id>` is present, and the Site doc's `d1_database_id` is a real CF uuid (not
   a hash-shaped placeholder).
2. **Migration applied** — `wrangler d1 execute paw-site-<site_id> --remote --command "SELECT name FROM sqlite_master WHERE type='table'"` lists `entries` (+ a `d1_migrations` ledger row).
3. **Read** — open the live `url`; the guestbook list renders server-side (empty first).
4. **Write** — submit the sign form; the page reflects the new row after the single-flight
   refresh. Re-query D1 to confirm the row landed.

If all four hold, **Phase 0 is proven.**

---

## 5. Troubleshooting map (symptom → cause → fix)

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Publish returns `sites.workers_dynamic_unsupported` | deploy mode is `workers`, not `wfp` | set `PAW_CF_DEPLOY_MODE=wfp` (checklist #1) |
| Site stuck in `provisioning` forever, job stays `queued` | arq worker not running | start the arq worker; check `POCKETPAW_REDIS_URL` (checklist #7) |
| Job `failed`, `provision_status="failed"`, no `d1_database_id` | `create_database` 403/401 | token lacks **D1:Edit** — fix `PAW_CF_API_TOKEN` scope (checklist #3) |
| Job `failed` **with** `d1_database_id` set, error `sites.migrate_*` | wrangler missing / bad creds / D1 not reachable | `sites.migrate_wrangler_missing` → checklist #5; else check `CLOUDFLARE_*` creds (#4). Retrying reuses the same D1 (no orphan). |
| Migrate error "no such database" | D1 create + migrate name mismatch, or WfP-vs-account D1 region | confirm the D1 is `paw-site-<site_id>` on the same account the token addresses |
| Read renders but write 500s | remote-fn/D1 write perms or schema drift | check the Worker logs; confirm the `entries` table matches the emitted `0001_init.sql` |
| Deploy step fails (`put_worker`) | dispatch namespace / Workers perms | checklist #2/#3; re-run the dispatch-setup runbook |

## 6. After the smoke

- A failed provision that created a D1 leaves it behind (the retry reuses it, but an abandoned
  site does not). Clean stray databases with
  `docs/runbooks/2026-07-09-dynamic-sites-orphan-d1-cleanup.md`.
- Record what broke + the fix here or in a follow-up — the first real run always surfaces
  real-world quirks the mocked unit tests can't (exact wrangler behavior, D1 API timing).
