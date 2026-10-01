#!/usr/bin/env bash
# scripts/vendor-paw-sites-allowlist.sh -- re-vendor paw-sites' dependency allowlist.
#
# Created 2026-10-02 (fix/canon-cross-repo-pins, CN-8). paw-sites owns the author
# dependency rules (src/allowlist.ts: VETTED_DEPENDENCIES, TOOLCHAIN_RESERVED,
# MAX_AUTHOR_PACKAGES) and prints them with `paw-sites-gen allowlist`. pocketpaw
# commits that output as ee/pocketpaw_ee/sites/paw-sites-allowlist.json, which
# vetted_pins and dependency_manifest read. This script runs the command at a ref
# (default origin/main) in a throwaway worktree, so the shared checkout is never
# touched, and rewrites the JSON plus paw-sites-allowlist.pin.json (commit + sha256).
# tests/ee/sites/test_paw_sites_allowlist_vendored.py fails when the two disagree.
#
# This is the COMMITTED copy. scripts/vendor-paw-sites.sh still writes the deploy
# copy (deploy/paw-sites/allowlist.json) that the image ships beside the generator.
#
# Usage:
#   scripts/vendor-paw-sites-allowlist.sh            # ../paw-sites at origin/main
#   PAW_SITES_DIR=/path/to/paw-sites PAW_SITES_REF=<ref> scripts/vendor-paw-sites-allowlist.sh
# paw-sites installs file:../ripple deps; RIPPLE_DIR overrides the sibling ../ripple.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${PAW_SITES_DIR:-$HERE/../paw-sites}"
REF="${PAW_SITES_REF:-origin/main}"
DEST="$HERE/ee/pocketpaw_ee/sites/paw-sites-allowlist.json"
PIN="$HERE/ee/pocketpaw_ee/sites/paw-sites-allowlist.pin.json"

[ -e "$SRC/.git" ] || { echo "ERROR: paw-sites checkout not found at '$SRC'" >&2; exit 1; }
git -C "$SRC" fetch -q origin || echo "[vendor-paw-sites-allowlist] WARN: fetch failed; using the local $REF" >&2
COMMIT="$(git -C "$SRC" rev-parse "$REF")"

WT="$(mktemp -d)/paw-sites"
trap 'git -C "$SRC" worktree remove --force "$WT" >/dev/null 2>&1 || true' EXIT
git -C "$SRC" worktree add -q --detach "$WT" "$COMMIT"
# paw-sites depends on file:../ripple/packages/*, so the worktree needs a ripple beside it.
ln -s "${RIPPLE_DIR:-$(cd "$SRC/.." && pwd)/ripple}" "$(dirname "$WT")/ripple"
( cd "$WT" && bun install --frozen-lockfile >/dev/null && bun src/cli.ts allowlist > "$DEST.tmp" )
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DEST.tmp"
mv "$DEST.tmp" "$DEST"

python3 - "$DEST" "$PIN" "$COMMIT" <<'PY'
import hashlib, json, sys
dest, pin, commit = sys.argv[1:]
data = json.load(open(pin, encoding="utf-8"))
data.update(source_commit=commit, sha256=hashlib.sha256(open(dest, "rb").read()).hexdigest())
open(pin, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
print(f"[vendor-paw-sites-allowlist] {commit[:7]} sha256 {data['sha256']}")
PY
