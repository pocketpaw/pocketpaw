# tests/ee/sites/test_paw_sites_allowlist_vendored.py — the vendored paw-sites
# allowlist is pinned, and the dependency rules come from it.
# Created 2026-10-02 (fix/canon-cross-repo-pins, CN-8). vetted_pins' fallback pins
# and dependency_manifest's TOOLCHAIN_RESERVED / MAX_DECLARED_PACKAGES used to be
# hand copies of paw-sites' allowlist.ts. They now read
# ee/pocketpaw_ee/sites/paw-sites-allowlist.json (``paw-sites-gen allowlist`` at
# the commit in paw-sites-allowlist.pin.json). Refresh with
# scripts/vendor-paw-sites-allowlist.sh.
"""Hash pin and derivation checks for the vendored paw-sites allowlist."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from pocketpaw_ee.sites import dependency_manifest as dm
from pocketpaw_ee.sites import vetted_pins

SITES = Path(vetted_pins.__file__).parent
PIN = json.loads((SITES / "paw-sites-allowlist.pin.json").read_text("utf-8"))


def test_the_vendored_allowlist_matches_its_pin():
    raw = (SITES / "paw-sites-allowlist.json").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == PIN["sha256"], (
        "paw-sites-allowlist.json drifted from its pin; run scripts/vendor-paw-sites-allowlist.sh"
    )
    assert PIN["source_repo"] == "qbtrix/paw-sites" and len(PIN["source_commit"]) == 40


def test_the_rules_are_read_from_the_vendored_allowlist():
    vendored = vetted_pins.VENDORED_ALLOWLIST
    assert dm.MAX_DECLARED_PACKAGES == vendored["maxAuthorPackages"]
    assert dm.TOOLCHAIN_RESERVED == tuple(
        e[:-1] if e.endswith("/*") else e for e in vendored["toolchainReserved"]
    )
    assert vetted_pins.FALLBACK_PINS == vendored["vetted"]
    # Scope entries keep their meaning across the '/*' -> '/' spelling change.
    assert dm.is_toolchain_reserved("@sveltejs/kit")
    assert not dm.is_toolchain_reserved("@sveltejs")


def _paw_sites_source_at_pin() -> str:
    parents = Path(__file__).resolve().parents
    env = os.environ.get("PAW_SITES_DIR")
    repo = (
        Path(env)
        if env
        else next((p / "paw-sites" for p in parents if (p / "paw-sites").is_dir()), None)
    )
    if repo is None:
        pytest.skip("paw-sites is not checked out beside this repo (set PAW_SITES_DIR)")
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "show", f"{PIN['source_commit']}:src/allowlist.ts"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        pytest.skip(f"{PIN['source_commit'][:7]} is not in {repo}; fetch it")


def test_the_vendored_rules_are_what_paw_sites_declared_at_the_pinned_commit():
    """Cross-repo: re-read allowlist.ts at the pinned commit. Skips without it."""
    source = _paw_sites_source_at_pin()
    block = re.search(r"TOOLCHAIN_RESERVED[^=]*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)
    assert block, "TOOLCHAIN_RESERVED not found in allowlist.ts"
    reserved = re.findall(r"'([^']+)'", block.group(1))
    cap = re.search(r"MAX_AUTHOR_PACKAGES\s*=\s*(\d+)", source)
    assert reserved == vetted_pins.VENDORED_ALLOWLIST["toolchainReserved"]
    assert cap and int(cap.group(1)) == vetted_pins.VENDORED_ALLOWLIST["maxAuthorPackages"]
