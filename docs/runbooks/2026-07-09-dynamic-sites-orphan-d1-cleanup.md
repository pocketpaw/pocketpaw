<!-- Runbook: clean up orphaned per-tenant D1 databases left by failed dynamic-site
     provisions (Phase 0). Created 2026-07-09 (DP0-6). Phase 0 ships this MANUAL
     procedure; an automated reaper is a Phase 1 follow-up. -->

# Runbook — Dynamic Paw Sites: orphaned D1 cleanup

## Why orphans happen

The `provision_site` job persists a new D1's id on the Site doc **immediately** after
`create_database`, before migrate/deploy. That is deliberate: if the job fails later and
**retries**, it reuses that same D1 instead of creating a second one. The trade-off is that a
D1 whose site is then abandoned (deleted, never retried, or the pocket removed) leaves a real,
billable Cloudflare database behind.

Phase 0 handles this **manually** (this runbook). An automated reaper is deferred to Phase 1.

## Find orphans

An orphan = a `paw-site-<id>` D1 on the Cloudflare account with no live/`provisioned` Site
backing it. Cross-reference two lists:

1. **Cloudflare side** — every D1 named `paw-site-*`:
   ```
   wrangler d1 list        # or the D1 dashboard; note each database_name + uuid
   ```
2. **Control-plane side** — the Site docs and their state. In Mongo:
   ```
   // sites where provisioning never completed
   db.sites.find(
     { provision_status: { $in: ["failed", "provisioning"] } },
     { _id: 1, workspace: 1, pocket_id: 1, d1_database_id: 1, provision_status: 1 }
   )
   // and sites that were archived/deleted but still carry a d1_database_id
   db.sites.find({ archived: true, d1_database_id: { $ne: "" } },
                 { _id: 1, d1_database_id: 1 })
   ```

A `paw-site-<id>` D1 whose `<id>` has **no** matching Site doc, or whose Site is `failed` /
archived and will not be retried, is a cleanup candidate.

## Before deleting — confirm it's truly dead

- The D1 name is `paw-site-<site_id>`; look up that `site_id` in `db.sites`.
- If the Site is `provisioning` and **recent**, it may be an in-flight job — do NOT delete; wait
  for the job to finish or fail.
- If the Site is `provisioned`/live, its D1 is **in use** — never delete.
- Only delete when the Site is `failed`/archived/absent AND you do not intend to retry the
  publish (a retry would recreate the schema in the same D1, so deleting forces a fresh create).

## Delete

```
wrangler d1 delete paw-site-<site_id>          # irreversible — data is gone
```

Then clear the stale pointer so a future publish provisions fresh (optional, if the Site doc
is being kept):
```
db.sites.updateOne(
  { _id: ObjectId("<site_id>") },
  { $set: { d1_database_id: "", provision_status: "none" } }
)
```

## Notes

- D1 delete is **destructive and irreversible** — double-check the `site_id` maps to a dead
  site before running it.
- Keep a short log of what was deleted (site_id, uuid, date, reason) until the automated reaper
  lands, so a mistaken delete is traceable.
