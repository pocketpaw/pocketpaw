#!/usr/bin/env bash
# scripts/vendor-paw-bar-loader.sh -- re-vendor the Paw Bar embed loader from paw-bar.
#
# Created 2026-10-02 (fix/canon-cross-repo-pins, CN-8). paw-bar owns the loader
# source (loader/src/loader.ts); pocketpaw serves a copy at GET /paw-bar/widget.js
# from ee/pocketpaw_ee/paw_bar/static/paw-bar.js. This script builds paw-bar at a ref
# (default origin/main) in a throwaway worktree, so the shared checkout is never
# touched, replaces everything below the vendored file's header with
# loader/dist/loader.readable.js, and rewrites ee/pocketpaw_ee/paw_bar/
# paw-bar-loader.pin.json with the source commit and the body's sha256.
# tests/cloud/test_paw_bar_widget_js.py fails when the body and the pin disagree.
#
# Usage:
#   scripts/vendor-paw-bar-loader.sh                 # ../paw-bar at origin/main
#   PAW_BAR_DIR=/path/to/paw-bar PAW_BAR_REF=<ref> scripts/vendor-paw-bar-loader.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${PAW_BAR_DIR:-$HERE/../paw-bar}"
REF="${PAW_BAR_REF:-origin/main}"
DEST="$HERE/ee/pocketpaw_ee/paw_bar/static/paw-bar.js"
PIN="$HERE/ee/pocketpaw_ee/paw_bar/paw-bar-loader.pin.json"

[ -d "$SRC/.git" ] || [ -f "$SRC/.git" ] || { echo "ERROR: paw-bar checkout not found at '$SRC'" >&2; exit 1; }
git -C "$SRC" fetch -q origin || echo "[vendor-paw-bar-loader] WARN: fetch failed; using the local $REF" >&2
COMMIT="$(git -C "$SRC" rev-parse "$REF")"

WT="$(mktemp -d)/paw-bar"
trap 'git -C "$SRC" worktree remove --force "$WT" >/dev/null 2>&1 || true' EXIT
git -C "$SRC" worktree add -q --detach "$WT" "$COMMIT"
( cd "$WT" && bun install --frozen-lockfile >/dev/null && node loader/build.mjs >/dev/null )

BUILT="$WT/loader/dist/loader.readable.js"
[ -s "$BUILT" ] || { echo "ERROR: build produced no $BUILT" >&2; exit 1; }

python3 - "$DEST" "$BUILT" "$PIN" "$COMMIT" <<'PY'
import hashlib, json, sys
dest, built, pin, commit = sys.argv[1:]
text = open(dest, encoding="utf-8").read()
header = text[: text.index('"use strict";')]
body = open(built, encoding="utf-8").read().replace("\r\n", "\n")
open(dest, "w", encoding="utf-8").write(header + body)
data = json.load(open(pin, encoding="utf-8"))
data.update(source_commit=commit, sha256=hashlib.sha256(body.encode()).hexdigest())
open(pin, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
print(f"[vendor-paw-bar-loader] {commit[:7]} sha256 {data['sha256']}")
PY
echo "[vendor-paw-bar-loader] done. Add a dated note to the header of $DEST if behaviour changed."
