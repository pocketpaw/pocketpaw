<!--
2026-09-24-sites-legacy-build-shell-migration.md
Created 2026-09-24 (fix/sites-legacy-build-shell-migration, PP-4): the operator runbook
for scripts/migrate_legacy_build_shell.py. Run it before deploying paw-sites #55.
-->

# Legacy build-shell migration (run BEFORE deploying paw-sites #55)

**Created:** 2026-09-24 · **Dry run by default.** `--apply` writes draft versions only.
It never publishes.

## Why

paw-sites #55 (PS-1) makes the generator own the build shell. It refuses a source map
that authors any of these files:

- svelte: `package.json`, `vite.config.{ts,js,mjs,mts,cjs,cts}`, `svelte.config.js`,
  `src/routes/+layout.{ts,js}`
- react: the other `vite.config.*` spellings
- every engine: a `paw.dependencies.json` under any spelling other than the exact
  canonical key
- install config: `bunfig.toml`, `.npmrc`, `bun.lock`, `bun.lockb`, `package-lock.json`

Matching ignores case and backslashes. Before PS-1 an authored copy of these files
won silently, so some existing pockets carry one. Once #55 is deployed, their next
build, publish or preview fails.

Until the migration has run, pocketpaw fails those builds with a 422
`sites.generator_owned_file` that names the file, not a generic `generator_failed`.
That makes the problem visible. It doesn't fix it. The edit tools refuse these
paths, so neither the agent nor the user can remove the file.

## What it does

Every generator-owned key gets one of three classes:

| Class | When | Apply does |
|---|---|---|
| `safe_drop` | The generator emits the equivalent anyway. That covers the stock plugins-only `vite.config`, the generator's `svelte.config.js` or a stricter subset of it, a `+layout.ts` that only sets `prerender`/`csr`/`ssr` flags matching the site's `keepsClientBundle`, a `package.json` that lists only toolchain packages at the generator's pins, lockfiles, and empty `bunfig.toml`/`.npmrc` | removes the file |
| `convertible` | The file declares npm packages: an authored `package.json`, or a misspelled manifest | vets each package with the PP-1 resolver, merges it into `paw.dependencies.json` (existing entries win), then removes the file |
| `needs_review` | Anything we can't prove is equivalent: layout logic (`load`, imports), csr/prerender flags that disagree with the site, custom scripts or plugins, off-pin toolchain versions, `overrides`, registry config, packages the resolver refuses, or svelte packages on a site without a client bundle | nothing. The file shows up in the report for a human |

A pocket's writes all land in one draft version, authored by
`system:legacy-build-shell-migration` and labelled "Moved build-shell files to the
generator". You can revert it from the version timeline. The live site keeps serving
its current deploy until the next publish.

## Steps

The Mongo URI comes from the environment, never from an argument. See the
[site-plan census runbook](2026-08-21-site-plan-census.md) for how to reach
production Mongo.

1. **Dry run and save the report.**

   ```bash
   POCKETPAW_CLOUD_MONGO_URI=... uv run python scripts/migrate_legacy_build_shell.py --out dry-run.json
   ```

   Add `--no-resolve` to skip the npm registry. Conversions then show up unresolved.

2. **Review it.** Look for `needs_review` rows (`pockets[].files[]` with
   `"class": "needs_review"`); each one has a `reason`. Fix what you can by hand.
   For example, set a pocket's `keepsClientBundle` to match its layout's `csr` flag,
   then run the dry run again. Anything left over needs the owner or support.

3. **Apply.**

   ```bash
   POCKETPAW_CLOUD_MONGO_URI=... uv run python scripts/migrate_legacy_build_shell.py --apply --out apply.json
   ```

   If a file was edited after the dry run, that pocket is skipped with
   `pocket.build_shell_changed` and shows up under `errors`. Run it again. The run
   is idempotent. If it stops, resume with `--after <last_pocket_id>` from the
   report.

4. **Check.** Run the dry run again. Only `needs_review` rows should be left.

5. **Deploy paw-sites #55.** Migrated sites pick up the change on their next publish.

## Flags

`--workspace <id>` limits the run to one workspace. `--batch-size`, `--limit` and
`--after` page through the pockets. `--json` prints the full report.

The script exits 1 if any pocket errored.
