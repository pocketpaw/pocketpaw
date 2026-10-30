#!/usr/bin/env bash
# scripts/vendor-ripple-tarball.sh -- pack the built @ripple-ui/svelte package into
# a resolvable npm TARBALL staged at deploy/ripple/ripple-ui-svelte-0.5.0.tgz, so
# Dockerfile.enterprise can satisfy a generated Paw Site's @ripple-ui/svelte
# dependency WITHOUT clone access to the private ripple repo (the
# RIPPLE_SOURCE=vendor path).
#
# Created 2026-06-25 (feat/paw-sites-prod-deploy, DEP-2). A generated ripple-track
# site pins @ripple-ui/svelte and, before ``bun install``, the Sites publish path
# rewrites that dep to ``PAW_SITES_RIPPLE_DEP`` — baked in the image as
# ``file:/opt/ripple-ui-svelte-0.5.0.tgz``. This script produces that tarball.
#
# Why a tarball (not a vendored dist tree like the FE): a generated site installs
# @ripple-ui/svelte as a NORMAL dependency from a ``file:`` spec; a ``file:`` to a
# packed .tgz is the resolvable, self-contained form bun installs cleanly. Packing
# (``bun pm pack``) respects ripple's package.json ``files`` field, so the tarball
# carries exactly the published surface (dist/ minus tests) + package.json.
#
# Source precedence:
#   1. $RIPPLE_DIR (explicit override) — e.g. a CI-downloaded checkout.
#   2. ../ripple (sibling checkout) — the workspace dev layout. Its dist/ is a
#      gitignored build artifact, so this script builds it first if missing.
#
# Usage:
#   scripts/vendor-ripple-tarball.sh                    # pack from ../ripple
#   RIPPLE_DIR=/path/to/ripple scripts/vendor-ripple-tarball.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/deploy/ripple"
SRC="${RIPPLE_DIR:-$HERE/../ripple}"
# Must match the file the Dockerfile bakes into PAW_SITES_RIPPLE_DEP and the
# version in ripple/package.json (@ripple-ui/svelte@0.5.0).
TARBALL="ripple-ui-svelte-0.5.0.tgz"

if [ ! -d "$SRC" ]; then
  echo "ERROR: ripple source not found at '$SRC'." >&2
  echo "Set RIPPLE_DIR to a ripple checkout." >&2
  exit 1
fi

# Build ripple's dist if it's absent (sibling checkout: dist/ is gitignored).
if [ ! -f "$SRC/dist/index.js" ]; then
  echo "[vendor-ripple-tarball] dist/ missing in '$SRC' — building it"
  ( cd "$SRC" && bun install --frozen-lockfile && bun run build )
fi

if [ ! -f "$SRC/dist/index.js" ]; then
  echo "ERROR: '$SRC/dist/index.js' still missing after build." >&2
  exit 1
fi

echo "[vendor-ripple-tarball] packing $SRC -> $DEST/$TARBALL"
rm -rf "$DEST"
mkdir -p "$DEST"
# ``bun pm pack`` writes the .tgz into the package dir; --destination targets DEST.
# The packed name is derived from name+version, so normalize to the expected file.
( cd "$SRC" && bun pm pack --destination "$DEST" >/dev/null )

# bun names the tarball "<scope>-<name>-<version>.tgz" (e.g.
# ripple-ui-svelte-0.5.0.tgz). Normalize whatever was produced to the exact name
# the Dockerfile/PAW_SITES_RIPPLE_DEP expects.
produced="$(ls -1 "$DEST"/*.tgz 2>/dev/null | head -n1 || true)"
if [ -z "$produced" ]; then
  echo "ERROR: no .tgz produced by 'bun pm pack' in '$DEST'." >&2
  exit 1
fi
if [ "$(basename "$produced")" != "$TARBALL" ]; then
  mv "$produced" "$DEST/$TARBALL"
fi

echo "[vendor-ripple-tarball] done. $DEST/$TARBALL is ready for a RIPPLE_SOURCE=vendor build."
