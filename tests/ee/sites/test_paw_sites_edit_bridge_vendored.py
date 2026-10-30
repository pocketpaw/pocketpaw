# tests/ee/sites/test_paw_sites_edit_bridge_vendored.py — the vendored paw-sites
# in-frame scripts (edit_bridge.js, runtime_reporter.js) are pinned to a paw-sites
# commit by sha256 (paw-sites-edit-bridge.pin.json). Refresh both with
# scripts/vendor-paw-sites-edit-bridge.sh; never hand-edit the .js files.
"""Hash pin and cross-repo checks for the vendored paw-sites edit bridge."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import pytest
from pocketpaw_ee.sites import preview_origin

SITES = Path(preview_origin.__file__).parent
PIN = json.loads((SITES / "paw-sites-edit-bridge.pin.json").read_text("utf-8"))
PLACEHOLDER = '"__PAW_BUILDER_ORIGIN__"'


def _body(name: str) -> bytes:
    return (SITES / name).read_bytes().replace(b"\r\n", b"\n")


@pytest.mark.parametrize("name", ["edit_bridge.js", "runtime_reporter.js"])
def test_the_vendored_script_matches_its_pin(name):
    assert hashlib.sha256(_body(name)).hexdigest() == PIN["sha256"][name], (
        f"{name} drifted from its pin; run scripts/vendor-paw-sites-edit-bridge.sh"
    )
    assert PIN["source_repo"] == "qbtrix/paw-sites" and len(PIN["source_commit"]) == 40
    assert PIN["source_commit"][:7] in _body(name).decode("utf-8")


def test_the_origin_placeholder_is_fully_replaced():
    origin = "https://dash.paw.example"
    for script in (
        preview_origin.edit_bridge_script(origin),
        preview_origin.runtime_reporter_script(origin),
    ):
        assert PLACEHOLDER not in script
        assert json.dumps(origin) in script
        assert not script.lstrip().startswith("//")
    bridge = preview_origin.edit_bridge_script(origin)
    # The bridge carries its own reporter and the command channel.
    assert "__pawRuntimeReporter" in bridge and "__pawEditCmd" in bridge


def test_the_vendored_scripts_are_what_paw_sites_emits_at_the_pin():
    """Cross-repo: regenerate at the pinned commit. Skips without paw-sites or bun."""
    parents = Path(__file__).resolve().parents
    env = os.environ.get("PAW_SITES_DIR")
    repo = (
        Path(env)
        if env
        else next((p / "paw-sites" for p in parents if (p / "paw-sites").is_dir()), None)
    )
    bun = shutil.which("bun")
    if repo is None or bun is None:
        pytest.skip("needs a paw-sites checkout beside this repo (PAW_SITES_DIR) and bun")
    try:
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", PIN["source_commit"], "src"],
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError:
        pytest.skip(f"{PIN['source_commit'][:7]} is not in {repo}; fetch it")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        Path(tmp, "print.ts").write_text(
            "import { editBridgeScript, runtimeReporterScript } from './src/edit-bridge.ts';\n"
            "const fn = process.argv[2] === 'bridge' ? editBridgeScript : runtimeReporterScript;\n"
            "process.stdout.write(fn('__PAW_BUILDER_ORIGIN__'));\n",
            encoding="utf-8",
        )
        for which, name in (("bridge", "edit_bridge.js"), ("reporter", "runtime_reporter.js")):
            emitted = subprocess.run(
                [bun, "print.ts", which], cwd=tmp, check=True, capture_output=True, text=True
            ).stdout
            assert preview_origin._vendored(name) == emitted.replace("\r\n", "\n"), name
