#!/usr/bin/env bash
# scripts/vendor-ripple-tarball.sh -- pack ripple's published packages into
# resolvable npm TARBALLS staged in deploy/ripple/, so Dockerfile.enterprise can
# satisfy a generated Paw Site's @ripple-ui/svelte dependency WITHOUT clone access
# to the private ripple repo (the RIPPLE_SOURCE=vendor path).
#
# Updated 2026-09-18 (chore/ripple-split-sites-dep): ripple is a bun workspace now
# (ripple-iui #119), so this packs TWO tarballs, one per package, and keeps the
# names `bun pm pack` gives them. Two things changed and both mattered:
#   * There is no root dist/ and no root package. Packing the root yields
#     `ripple-iui-0.0.0.tgz` — the entire private monorepo.
#   * The old code renamed whatever came out to the @ripple-ui/svelte filename.
#     Post-split that would have staged the monorepo tarball under the svelte name,
#     and every downstream check (this script's, the Dockerfile's `test -f`) would
#     have gone green on a package that cannot install.
#   * @ripple-ui/core is now a separate package, and the svelte tarball declares it
#     as `file:../core` — a path bun resolves against its own install cache rather
#     than the tarball, so the consumer gets no engine. The image redirects that
#     spec to the core tarball via PAW_SITES_RIPPLE_CORE_DEP.
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
# Must match the files the Dockerfile bakes into PAW_SITES_RIPPLE_DEP /
# PAW_SITES_RIPPLE_CORE_DEP and the versions in ripple's packages/{svelte,core}.
# These are EXPECTED names, not rename targets — see the header.
TARBALL="ripple-ui-svelte-0.7.0.tgz"
CORE_TARBALL="ripple-ui-core-0.5.0.tgz"

if [ ! -d "$SRC" ]; then
  echo "ERROR: ripple source not found at '$SRC'." >&2
  echo "Set RIPPLE_DIR to a ripple checkout." >&2
  exit 1
fi

# Build ripple's dists if absent (sibling checkout: dist/ is gitignored). The build
# runs at the workspace ROOT — `bun run build` there fans out to every package —
# then the install is re-run, matching the vendor-to-workspace.yml sequence (the
# first install predates either dist/).
if [ ! -f "$SRC/packages/svelte/dist/index.js" ] || [ ! -f "$SRC/packages/core/dist/index.js" ]; then
  echo "[vendor-ripple-tarball] packages/*/dist missing in '$SRC' — building them"
  ( cd "$SRC" && bun install --frozen-lockfile && bun run build && bun install --frozen-lockfile )
fi

for entry in packages/svelte packages/core; do
  if [ ! -f "$SRC/$entry/dist/index.js" ]; then
    echo "ERROR: '$SRC/$entry/dist/index.js' still missing after build." >&2
    exit 1
  fi
done

echo "[vendor-ripple-tarball] packing $SRC/packages/{svelte,core} -> $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
# ``bun pm pack`` writes the .tgz into the package dir; --destination targets DEST.
# Packed PER PACKAGE: the repo root is private and packs as ripple-iui-0.0.0.tgz.
( cd "$SRC/packages/svelte" && bun pm pack --destination "$DEST" >/dev/null )
( cd "$SRC/packages/core" && bun pm pack --destination "$DEST" >/dev/null )

# bun names each tarball "<scope>-<name>-<version>.tgz". NOTHING is renamed: the
# name is the check. An unexpected one means ripple's version moved and the image's
# COPY + ENV have to move with it, so fail loudly instead of mislabelling.
for tarball in "$TARBALL" "$CORE_TARBALL"; do
  if [ ! -f "$DEST/$tarball" ]; then
    echo "ERROR: 'bun pm pack' did not produce '$tarball' in '$DEST'. Got:" >&2
    ls -1 "$DEST" >&2 || true
    echo "Bump TARBALL / CORE_TARBALL here AND the ARG + COPY + ENV lines in Dockerfile.enterprise." >&2
    exit 1
  fi
done

echo "[vendor-ripple-tarball] done. $DEST/{$TARBALL,$CORE_TARBALL} are ready for a RIPPLE_SOURCE=vendor build."
