# tests/ee/sites/test_local_deploy_integration.py — the REAL Phase 3 proof.
#
# Unlike test_dentist_e2e.py (generator + CF faked), this test drives the FULL
# local publish pipeline with NOTHING faked on the build side:
#   real generator (node dist/cli.js)  →  bun install (ripple from the tarball)
#   →  smoke `bun run build`  →  LOCAL fake-deploy  →  the per-site static server
#   serves the prerendered HTML  →  curl/GET confirms "Bright Smile Dental".
# This is exactly the thing Phase 5 (cmux smoke) depends on: a publish with no
# Cloudflare creds yields a locally-served, openable site.
#
# It spawns real bun + node and takes ~30-40s, so it is OPT-IN: it skips unless
# PAW_SITES_INTEGRATION=1 is set AND the toolchain (node, bun), the built
# generator dist, and the local ripple tarball are all present. The default
# `uv run pytest tests/ee/sites/` therefore stays fast (unit-only); CI on a box
# without bun/node skips cleanly.
#
# Env knobs the test sets for itself (mirrors the prod TODO shims):
#   PAW_SITES_GEN_CMD     = "node <paw-sites>/dist/cli.js"   (bin not installed)
#   PAW_SITES_RIPPLE_DEP  = "file:<tarball>"                 (ripple unpublished)
#   PAW_SITES_LOCAL_DIR   = a tmp dir                        (don't touch ~/.pocketpaw)
#   PAW_SITES_LOCAL       = "1"                              (force local branch)
#   PAW_CF_ACCOUNT_ID     unset                              (no Cloudflare)
#
# Created: 2026-06-01 (feat/paw-sites-backend, Phase 3 — local fake-deploy proof).

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

import pytest

# Repo layout: this file is at
# <worktree>/tests/ee/sites/test_local_deploy_integration.py and the generator
# lives in the sibling workspace checkout paw-workspace/paw-sites. We resolve it
# relative to a known anchor rather than hardcoding the absolute worktree path.
_PAW_SITES_DIR = Path.home() / "Documents" / "paw-workspace" / "paw-sites"
_GEN_CLI = _PAW_SITES_DIR / "dist" / "cli.js"
_RIPPLE_TARBALL = Path("/tmp/ripple-ui-svelte-0.2.0.tgz")


def _integration_enabled() -> tuple[bool, str]:
    import os

    if os.environ.get("PAW_SITES_INTEGRATION") != "1":
        return False, "set PAW_SITES_INTEGRATION=1 to run the local-deploy integration test"
    if shutil.which("node") is None:
        return False, "node not on PATH"
    if shutil.which("bun") is None:
        return False, "bun not on PATH"
    if not _GEN_CLI.is_file():
        return False, f"generator dist not built at {_GEN_CLI} (run `bun run build` in paw-sites)"
    if not _RIPPLE_TARBALL.is_file():
        return False, f"ripple tarball missing at {_RIPPLE_TARBALL}"
    return True, ""


_enabled, _skip_reason = _integration_enabled()
pytestmark = pytest.mark.skipif(not _enabled, reason=_skip_reason)


# The dentist marketing spec — a heading the test asserts on, plus the lead form.
_DENTIST_SPEC = {
    "type": "container",
    "children": [
        {"type": "heading", "props": {"text": "Bright Smile Dental"}},
        {"type": "text", "props": {"text": "Modern dentistry in downtown Reno."}},
        {
            "type": "container",
            "children": [
                {"type": "input", "props": {"name": "full_name", "label": "Your name"}},
                {"type": "input", "props": {"name": "phone", "label": "Phone"}},
                {
                    "type": "button",
                    "props": {
                        "label": "Request appointment",
                        "variant": "primary",
                        "type": "submit",
                    },
                },
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_local_publish_serves_dentist_site_over_http(beanie_test_db, monkeypatch, tmp_path):
    """No CF creds → publish() runs the real generate → bun install → smoke build
    → local deploy chain and returns a localhost URL that actually serves the
    prerendered dentist HTML."""
    from pocketpaw_ee.sites import service as sites_service

    # Point the generator at the uninstalled dist and ripple at the local tarball;
    # force the local branch; persist under a throwaway dir.
    monkeypatch.setenv("PAW_SITES_GEN_CMD", f"node {_GEN_CLI}")
    monkeypatch.setenv("PAW_SITES_RIPPLE_DEP", f"file:{_RIPPLE_TARBALL}")
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    # NB: NOTHING is injected — real generator, real local deployer, no CF.
    site = await sites_service.publish(
        workspace_id="ws_dentist",
        user_id="freelancer_1",
        pocket_id="pk_dentist",
        ripple_spec=_DENTIST_SPEC,
        theme={"primary": "#0A84FF", "mode": "light"},
        name="Bright Smile Dental",
    )

    assert site.deployed is True
    # The deployed URL is a localhost address pointing at the site id.
    assert site.url.startswith("http://127.0.0.1:")
    assert site.url.endswith(f"/{site.id}/")

    # The served page is the prerendered dentist site — fetch it over HTTP and
    # confirm the marketing content is in the HTML (this is what cmux opens).
    with urllib.request.urlopen(site.url, timeout=10) as resp:  # noqa: S310 - localhost only
        assert resp.status == 200
        html = resp.read().decode()
    assert "Bright Smile Dental" in html
    # The lead form rendered too (the inputs the spec declared).
    assert 'name="full_name"' in html
    assert 'name="phone"' in html
    # Fully static: under csr=false the client JS chunks are pruned, so the HTML
    # must not reference them (only the kept CSS under _app/immutable/assets).
    assert "_app/immutable/chunks/" not in html
