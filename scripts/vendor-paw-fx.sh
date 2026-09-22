#!/usr/bin/env bash
# scripts/vendor-paw-fx.sh -- stage the built paw-fx effects registry into
# deploy/paw-fx/registry so Dockerfile.enterprise COPYs it to /opt/paw-fx/registry
# (read by the pocketpaw_fx MCP server via PAW_FX_REGISTRY_DIR).
#
# Created 2026-09-06 (feat/fx-mcp-server). Source precedence mirrors
# vendor-paw-sites.sh: $PAW_FX_DIR, else the sibling ../paw-fx checkout. Expects
# <src>/dist/registry/registry.json to exist (run paw-fx's build first).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${PAW_FX_DIR:-$HERE/../paw-fx}/dist/registry"
DEST="$HERE/deploy/paw-fx/registry"

[ -f "$SRC/registry.json" ] || { echo "ERROR: $SRC/registry.json not found (build paw-fx first)" >&2; exit 1; }
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
echo "vendored paw-fx registry -> $DEST ($(python3 -c "import json;print(len(json.load(open('$DEST/registry.json'))['items']))") items)"
