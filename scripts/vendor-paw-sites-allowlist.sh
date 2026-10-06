#!/usr/bin/env bash
# scripts/vendor-paw-sites-allowlist.sh -- re-vendor paw-sites' dependency allowlist.
#
# paw-sites owns the author dependency rules (src/allowlist.ts: VETTED_DEPENDENCIES,
# AUTHOR_DECLARABLE, the retired-and-empty TOOLCHAIN_RESERVED) and prints them with
# `paw-sites-gen allowlist`; maxAuthorPackages is printed as null (no cap). pocketpaw
# commits that output as ee/pocketpaw_ee/sites/paw-sites-allowlist.json, which
# vetted_pins and dependency_manifest read. This script rewrites the JSON plus
# paw-sites-allowlist.pin.json (commit + sha256);
# tests/ee/sites/test_paw_sites_allowlist_vendored.py fails when the two disagree.
#
# No worktree and no `bun install`: `git archive <commit> src` goes into a temp dir
# and bun imports allowlist.ts on its own (its import closure is node:module + errors.ts, no packages), so no ripple
# checkout is needed. The print below mirrors runAllowlist() in paw-sites
# src/cli.ts key for key; if that function's output changes, change this too (the
# output matched `paw-sites-gen allowlist` byte for byte at 170a273).
#
# This is the COMMITTED copy. scripts/vendor-paw-sites.sh still writes the deploy
# copy (deploy/paw-sites/allowlist.json) that the image ships beside the generator.
#
# Usage:
#   scripts/vendor-paw-sites-allowlist.sh            # ../paw-sites at origin/main
#   PAW_SITES_DIR=/path/to/paw-sites PAW_SITES_REF=<ref> scripts/vendor-paw-sites-allowlist.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${PAW_SITES_DIR:-$HERE/../paw-sites}"
REF="${PAW_SITES_REF:-origin/main}"
DEST="$HERE/ee/pocketpaw_ee/sites/paw-sites-allowlist.json"
PIN="$HERE/ee/pocketpaw_ee/sites/paw-sites-allowlist.pin.json"

[ -e "$SRC/.git" ] || { echo "ERROR: paw-sites checkout not found at '$SRC'" >&2; exit 1; }
git -C "$SRC" fetch -q origin || echo "[vendor-paw-sites-allowlist] WARN: fetch failed; using the local $REF" >&2
COMMIT="$(git -C "$SRC" rev-parse "$REF")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "$DEST.tmp"' EXIT
git -C "$SRC" archive "$COMMIT" src | tar -x -C "$TMP"
cat > "$TMP/print.ts" <<'TS'
import {
  AUTHOR_DECLARABLE,
  ENGINE_TOOLCHAIN_RESERVED,
  TOOLCHAIN_RESERVED,
  VETTED_DEPENDENCIES
} from './src/allowlist.ts';

const pinned: Record<string, string> = {};
for (const name of AUTHOR_DECLARABLE) pinned[name] = VETTED_DEPENDENCIES[name]!;
console.log(
  JSON.stringify({
    authorDeclarable: AUTHOR_DECLARABLE,
    pinned,
    toolchainReserved: TOOLCHAIN_RESERVED,
    engineToolchainReserved: ENGINE_TOOLCHAIN_RESERVED,
    maxAuthorPackages: null,
    vetted: VETTED_DEPENDENCIES
  })
);
TS
( cd "$TMP" && bun print.ts ) > "$DEST.tmp"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DEST.tmp"
mv "$DEST.tmp" "$DEST"

python3 - "$DEST" "$PIN" "$COMMIT" <<'PY'
import hashlib, json, sys
dest, pin, commit = sys.argv[1:]
data = json.load(open(pin, encoding="utf-8"))
data.update(source_commit=commit, sha256=hashlib.sha256(open(dest, "rb").read()).hexdigest())
open(pin, "w", encoding="utf-8", newline="\n").write(json.dumps(data, indent=2) + "\n")
print(f"[vendor-paw-sites-allowlist] {commit[:7]} sha256 {data['sha256']}")
PY
