# Site-plan census — what production actually holds

**Created:** 2026-08-21 · **Read-only.** Every command here is an aggregate or a
count. Nothing writes. **Run it before the per-site plan rekey, and again before
per-site billing enforcement is switched on.**

## Why

Two questions, and only the first one is about the rekey.

**1. Is the rekey a migration or a rename?** `publish_pocket` stamps
`Site.plan_tier` even when checkout degrades, so a paid-tier *value* can be
persisted with `subscription_status="none"` and no money behind it. If production
holds only floor-tier rows, renaming `basic`/`pro`/`business` to
`free`/`site`/`staff` is a rename. If it holds paid keys, it is a data migration,
and any of those rows carrying a custom domain drops to the free floor's
allowance the moment it runs — which is a customer-comms line, not a code change.

**2. Who does the cap bite when enforcement goes on?** Free includes a custom
domain on one site. Enforcement is attach-time only and never retroactive, so a
workspace already over that cap keeps every domain it has — but its *next* attach
is refused. Support wants that number before the flag flips, not after.

## Where production is

Production Mongo is the `paw-mongo` container inside the Coolify deployment
(`deploy/coolify/docker-compose.yaml`, service `mongo`, database
`paw-enterprise`). It publishes no port, so it is reachable only from that host's
docker network. There is no connection string in this repo and none on a dev
machine — the only `CLOUD_MONGODB_URI` configured anywhere in the workspace points
at localhost. Both routes below therefore start on the deployment host.

## Route A — on the deployment host (no Python needed)

Find the container first; Coolify may suffix the name.

```bash
docker ps --filter name=mongo --format '{{.Names}}'
```

Then, substituting the name if it is not `paw-mongo`:

```bash
docker exec paw-mongo mongosh --quiet paw-enterprise --eval '
const live = { archived: { $ne: true } };
print("sites total : " + db.sites.countDocuments({}));
print("sites live  : " + db.sites.countDocuments(live));
print("archived    : " + db.sites.countDocuments({ archived: true }));

print("\n--- Q1: plan_tier x subscription_status (live only) ---");
printjson(db.sites.aggregate([
  { $match: live },
  { $group: {
      _id: {
        plan_tier: { $ifNull: ["$plan_tier", "<unset>"] },
        subscription_status: { $ifNull: ["$subscription_status", "<unset>"] }
      },
      sites: { $sum: 1 },
      sites_with_domains: { $sum: { $cond: [ { $gt: [ { $size: { $ifNull: ["$domains", []] } }, 0 ] }, 1, 0 ] } },
      hostnames: { $sum: { $size: { $ifNull: ["$domains", []] } } }
  } },
  { $sort: { "_id.plan_tier": 1, "_id.subscription_status": 1 } }
]).toArray());

print("\n--- Q2: per workspace, sites already holding a domain ---");
printjson(db.sites.aggregate([
  { $match: live },
  { $project: {
      workspace: 1,
      hostnames: { $size: { $ifNull: ["$domains", []] } },
      paid: { $cond: [ { $eq: [ { $ifNull: ["$subscription_status", "none"] }, "active" ] }, 1, 0 ] }
  } },
  { $match: { hostnames: { $gt: 0 } } },
  { $group: {
      _id: "$workspace",
      domained_sites: { $sum: 1 },
      floor_domained_sites: { $sum: { $cond: [ { $eq: ["$paid", 0] }, 1, 0 ] } },
      max_hostnames_on_one_site: { $max: "$hostnames" }
  } },
  { $sort: { floor_domained_sites: -1 } }
]).toArray());
'
```

The `--eval` body is single-quoted, so the JavaScript inside uses only double
quotes. Keep it that way when editing, or the shell will end the string early and
mongosh will receive a fragment.

## Route B — anywhere that can reach the database

`scripts/census_site_plans.py` runs the same two aggregations and interprets them,
which Route A leaves to the reader.

```bash
CLOUD_MONGODB_URI=... uv run python scripts/census_site_plans.py
CLOUD_MONGODB_URI=... uv run python scripts/census_site_plans.py --json
```

The URI comes from the environment and never from an argument: a production
connection string in a command line ends up in shell history. The script prints
the database name and a coarse host class, never the URI.

## Reading the result

**Q1 — the grid.** `<unset>` in the `plan_tier` column is a pre-BC-9 row or a
first publish; it resolves to the floor and migrates with `basic`. Add up every
row whose `plan_tier` is neither the floor key nor `<unset>`:

- **Zero.** The rekey is a rename. PW-4 ships the mapping and nothing else.
- **Non-zero, none with `subscription_status: "active"`.** A migration with no
  refund exposure, because no per-site subscription has ever charged
  (`POCKETPAW_DODO_SITE_PRODUCTS` is configured nowhere). If any of those rows has
  `sites_with_domains > 0`, PW-4 gains a comms line: the domain keeps working, but
  the site lands on the free floor's allowance.
- **Non-zero, some active.** Stop and escalate. Real subscriptions exist and the
  ladder change is a pricing decision before it is a migration.

**Q2 — the per-workspace rows.** `floor_domained_sites` is how many of that
workspace's sites spend the free allowance: holding at least one domain and *not*
on an active paid subscription. That predicate is
`entitlements.site_domain_allowance(...) is not None` written in Mongo, and it is
the same one `sites.service._domain_cap_exceeded` applies at attach time.

- Any workspace with `floor_domained_sites > 1` is **already over** the cap of 1.
  It loses nothing when the flag flips; it is refused on its next attach. Count
  them and decide whether that population needs telling.
- `max_hostnames_on_one_site` above 2 means some site is already past
  `_FREE_MAX_HOSTNAMES_PER_SITE`. Same story: kept, not detached, refused next
  time. If the number is common rather than rare, that constant wants raising.

## Recording the answer

Write the counts and the date into the open-questions section of
`docs/design/drafts/2026-08-21-paw-sites-plan-wiring-prd.md` in paw-workspace.
PW-4 reads it from there.

## Known result

**Local dev database, 2026-08-21:** 43 sites, all live, none archived. 40 on
`basic`, 3 with `plan_tier` unset, every one of them `subscription_status: "none"`.
Zero non-floor rows, so on dev the rekey is a rename. Three sites in one workspace
each hold a domain, which puts that workspace over the cap of 1 already — on dev,
with one hostname apiece. **These are dev numbers and say nothing about
production.** They are recorded only as proof the queries run and as a worked
example of what the output looks like.

## The rekey happened — and the migration is optional

**Updated 2026-08-22 (feat/site-pricing-ladder).** The rekey shipped:
`basic`/`pro`/`business` are now `free`/`site`/`staff`, and the catalog gained two
per-ORG flats (`studio`, `agency`) that are not legal `Site.plan_tier` values at
all.

**It did not wait for these numbers, and it did not need to.** The catalog
resolves every legacy key permanently through `_LEGACY_SITE_TIER_ALIASES`, mapped
by capability rather than ladder position — `pro` sells what `site` sells,
`business` sells what `staff` sells. A database that is never touched keeps
working: the entitlement resolver answers identically for the old key and the new
one, and `_dodo_product_for` looks through the alias so an environment still keyed
`{"pro": ..., "business": ...}` keeps opening checkouts.

So question 1 above is no longer a gate. It is now just "how many rows will the
tidy-up move", and the answer changes nothing about whether it is safe.

### Running the migration

```bash
# Dry run — connects, counts, prints what it WOULD change, writes nothing.
CLOUD_MONGODB_URI=... uv run python scripts/migrate_site_plan_keys.py

# Then, once the dry run reads right:
CLOUD_MONGODB_URI=... uv run python scripts/migrate_site_plan_keys.py --apply
```

Re-running is a no-op: each update filters on the OLD value, so a second pass
matches nothing. Verify with the census above — a migrated database reports zero
rows on the legacy keys.

**What it deliberately will not touch:** any `plan_tier` that is neither a legacy
key nor a current one. An unrecognised value is reported and left exactly as
found, because rewriting an unplanned value to the floor silently revokes
whatever it was granting, and the script cannot tell a typo from a restored
backup written by a newer schema.

**What it buys:** reads stop going through an alias and dashboards stop showing a
key the catalog no longer lists. Nothing more. The aliases stay in the code
afterwards — they are permanent, not transitional, because a restored backup or a
replayed webhook can still present one years from now.
