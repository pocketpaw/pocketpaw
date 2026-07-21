# Vendored: composable SvelteKit template + recipe engine

Vendored 2026-07-22 (CS-1) from the `sveltekit-template` working tree.
Upstream layout and contracts are documented in that repo's `README.md`.

This directory is **generated project source**, not application code. Every
project Code Mode scaffolds is composed from it, so a file that goes missing
here goes missing from every user's project — and the symptom shows up inside
their VM, a long way from the cause.

## What was taken

| Path | Why |
|---|---|
| `base/` | The SvelteKit + Cloudflare Workers + D1 starter, with the anchor markers recipes splice into |
| `recipes/` | `db`, `auth`, `stripe` — manifest + `files/` payload each |
| `recipe-engine/` | Source-map read/apply, dependency order, splicing, migration numbering |

## What was deliberately left behind

| Path | Why |
|---|---|
| `node_modules/` | 400 MB, and not needed — see "How it runs" |
| `tests/` | They test the template itself and need vitest; our coverage is `tests/cloud/test_codescaffold.py` |
| `base/worker-configuration.d.ts` | 550 KB of generated Cloudflare types; the engine's `DEFAULT_IGNORE` skips it, so it can never reach a composed project |
| `base/pnpm-lock.yaml` | Also in `DEFAULT_IGNORE`, so likewise unreachable |
| `base/.svelte-kit/` | Build output that leaked in on the first copy. The engine ignores it, but it was 1.7 MB of dead weight in the wheel |

Rule of thumb when refreshing: if `readSourceMap`'s `DEFAULT_IGNORE` skips it,
it cannot reach a generated project, so it does not belong here.

## How it runs — no dependencies at all

`_runner/compose.mjs` is ours, not upstream. It is the single entry point the
Python side invokes; it prints the composed project as JSON on stdout and writes
nothing to disk.

```
node --no-warnings --experimental-strip-types \
     --import <file:// URL of _runner/register.mjs> \
     _runner/compose.mjs auth stripe
```

The engine is TypeScript, but it needs **no `tsx`, no `node_modules`, and no
bundler**. Node strips the types; `_runner/hooks.mjs` (15 lines) supplies the one
thing Node lacks — mapping TypeScript's `./x.js` import convention onto the
`./x.ts` that actually exists. The composed output was verified byte-identical to
a `tsx`-driven run before this approach was adopted.

The cost is a floor of **Node 22.6** (`--experimental-strip-types`). The
alternative was vendoring `tsx`, which bundles a per-platform esbuild binary —
a native artifact inside a Python wheel, chosen for the wrong OS half the time.

Two Windows-specific details, both learned the hard way and both covered by
tests: `--import` takes a `file://` URL (an absolute Windows path parses as the
scheme `d:`), and node must be resolved with `shutil.which` rather than spawned
as a bare `"node"`.

## Refreshing

1. Re-copy `base/`, `recipes/`, `recipe-engine/`, minus the exclusions above.
2. Keep `_runner/` — it is not upstream.
3. If a recipe was added or removed, update **both** catalogs:
   `_runner/compose.mjs`'s `MANIFESTS` and `codescaffold/domain.py`'s `CATALOG`.
   `test_catalog_matches_the_engines_manifest_list` fails if they drift.
4. Run `uv run pytest tests/cloud/test_codescaffold.py`. The compose tests shell
   the real engine, so they catch a broken vendor immediately.

## Do not add an ignore rule that matches this tree

`.gitignore` and `.dockerignore` both carry an explicit negation for this path.
Either layer can silently swallow a vendored file, and the failure is a 5xx with
nothing pointing at the cause. Both are verified: `git ls-files` and a real
`docker build` context inspection both report the full file count.
