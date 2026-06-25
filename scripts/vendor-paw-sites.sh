#!/usr/bin/env bash
# scripts/vendor-paw-sites.sh -- stage the prebuilt paw-sites GENERATOR into
# deploy/paw-sites/ so Dockerfile.enterprise can build the enterprise image
# WITHOUT clone access to the private paw-sites repo (the PAW_SITES_SOURCE=vendor
# path, mirroring paw-enterprise/scripts/vendor-ripple.sh).
#
# Created 2026-06-25 (feat/paw-sites-prod-deploy, DEP-4). The Sites publish path
# shells out to ``paw-sites-gen`` (the generator CLI) at publish time; the
# enterprise image needs that binary on PATH. paw-sites is a SIBLING repo OUTSIDE
# the pocketpaw build context, so a plain ``COPY ../paw-sites`` is impossible —
# this script copies the minimal runtime tree (built ``dist/`` + ``templates/`` +
# ``package.json``) into ``deploy/paw-sites/``, which the Dockerfile COPYs to
# ``/opt/paw-sites`` and symlinks as ``/usr/local/bin/paw-sites-gen``.
#
# What the runtime tree needs (and why this minimal set is enough):
#   * dist/        — the compiled generator (``tsc`` output; ``dist/cli.js`` is the
#                    bin). Its own top-level imports are node: stdlib + local ./
#                    modules ONLY — it has NO external runtime deps, so no
#                    node_modules is shipped.
#   * templates/   — the generator resolves these at RUNTIME via
#                    ``join(dirname(import.meta.url), '..', 'templates')`` from
#                    dist/, so templates/ MUST sit as a sibling of dist/ under
#                    /opt/paw-sites. Omitting it breaks every generate.
#   * package.json — carries the ``bin`` map + ``type: module``; harmless to ship.
#
# Source precedence:
#   1. $PAW_SITES_DIR (explicit override) — e.g. a CI-downloaded checkout.
#   2. ../paw-sites (sibling checkout) — the workspace dev layout. Its dist/ is a
#      gitignored build artifact, so this script runs the build if dist is missing.
#
# Usage:
#   scripts/vendor-paw-sites.sh                          # vendor from ../paw-sites
#   PAW_SITES_DIR=/path/to/paw-sites scripts/vendor-paw-sites.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/deploy/paw-sites"
SRC="${PAW_SITES_DIR:-$HERE/../paw-sites}"

if [ ! -d "$SRC" ]; then
  echo "ERROR: paw-sites source not found at '$SRC'." >&2
  echo "Set PAW_SITES_DIR to a paw-sites checkout." >&2
  exit 1
fi

# Build the generator's dist if it's absent (sibling checkout: dist/ is gitignored).
if [ ! -f "$SRC/dist/cli.js" ]; then
  echo "[vendor-paw-sites] dist/ missing in '$SRC' — building it"
  ( cd "$SRC" && bun install --frozen-lockfile && bun run build )
fi

if [ ! -f "$SRC/dist/cli.js" ]; then
  echo "ERROR: '$SRC/dist/cli.js' still missing after build." >&2
  exit 1
fi

if [ ! -d "$SRC/templates" ]; then
  echo "ERROR: '$SRC/templates' missing — the generator reads these at runtime." >&2
  exit 1
fi

echo "[vendor-paw-sites] vendoring $SRC -> $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
cp "$SRC/package.json" "$DEST/package.json"
cp -R "$SRC/dist" "$DEST/dist"
cp -R "$SRC/templates" "$DEST/templates"

echo "[vendor-paw-sites] done. deploy/paw-sites/ is ready for a PAW_SITES_SOURCE=vendor build."
