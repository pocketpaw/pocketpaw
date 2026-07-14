# tests/ee/sites/test_html_e2e.py — the served-artifact END-TO-END for the html
# track (HE-10). Created 2026-07-12 (feat/html-engine integration).
#
# This is the seam the team-soul guardrail insists on: source-level + generator
# tests are NOT sufficient — the pipeline spans repos (pocketpaw + the paw-sites
# generator subprocess), and every source-level test can pass while the ACTUALLY
# SERVED artifact is wrong. So this drives the REAL combined flow:
#
#   create_html_site (persist engine="html" pocket)
#     -> publish_pocket  (real paw-sites generator via PAW_SITES_GEN_CMD; html
#                         skips the Node build — HE-3)
#     -> local deploy    (PAW_CF_DEPLOY_MODE=local -> local_server.deploy_local)
#     -> assert on the SERVED file (byte-identical to the authored index.html,
#        and NO SvelteKit scaffold — proving the html path, not ripple/svelte)
#
# It shells out to the real generator (node + paw-sites/dist/cli.js), so it is
# skipped when PAW_SITES_GEN_CMD is not wired to a runnable generator (CI without
# the built generator). Where the generator IS available it is the load-bearing
# proof that the whole html chain works end to end.

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


def _generator_available() -> bool:
    """True when PAW_SITES_GEN_CMD resolves to a runnable generator that knows the
    html engine (so the E2E can actually build). Skips cleanly otherwise."""
    cmd = os.environ.get("PAW_SITES_GEN_CMD", "").strip()
    if not cmd:
        return False
    argv = shlex.split(cmd, posix=False)
    if not argv:
        return False
    exe = argv[0].strip('"')
    if not (shutil.which(exe) or Path(exe).exists()):
        return False
    try:
        # `build` with no args exits 2 with a usage line if the CLI loads.
        out = subprocess.run(
            [*[a.strip('"') for a in argv], "build"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        return False
    return "usage:" in (out.stderr + out.stdout).lower()


pytestmark = pytest.mark.skipif(
    not _generator_available(),
    reason="PAW_SITES_GEN_CMD is not wired to a runnable paw-sites generator",
)


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """create + publish gate on the workspace Sites plan; default it to one that
    unlocks Sites ('go') for these synthetic-id E2E runs."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


@pytest.fixture()
def recording_bus():
    """A recording EventBus so create/publish emits don't raise (the real bus is
    only wired by init_realtime())."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list = []

        async def publish(self, event) -> None:  # noqa: ANN001
            self.events.append(event)

        def subscribe(self, event_type, handler) -> None:  # noqa: ANN001, ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield
    bus_mod._bus = prev  # type: ignore[attr-defined]


def _authored_source() -> dict[str, str]:
    return {
        "index.html": (
            '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8" />\n'
            '  <link rel="stylesheet" href="styles.css" />\n  <title>Bright Smile Dental</title>\n'
            "</head>\n<body>\n"
            '  <header><h1>Bright Smile Dental</h1><a href="#book">Book</a></header>\n'
            '  <main id="book"><h2>Appointments</h2>\n'
            '    <form method="post" action="/api/submit">\n'
            '      <input name="name" placeholder="Your name" />\n'
            '      <input name="email" type="email" placeholder="Email" />\n'
            '      <button type="submit">Request</button>\n    </form>\n  </main>\n'
            "</body>\n</html>\n"
        ),
        "styles.css": "body{font-family:system-ui;margin:0;color:#17130f}header{padding:2rem}",
    }


@pytest.mark.asyncio
async def test_create_publish_serves_authored_html(
    beanie_test_db, recording_bus, tmp_path, monkeypatch
):
    """The full chain: an agent creates an html site, publishes it, and the LOCALLY
    SERVED artifact is byte-identical to the authored index.html — with no SvelteKit
    scaffold anywhere in the deployed tree (proving the html path, not ripple/svelte)."""
    from bson import ObjectId
    from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
    from pocketpaw_ee.sites import service as sites_service

    # Local deploy: serve to a temp dir on a temp port, no Cloudflare.
    served_root = tmp_path / "served"
    served_root.mkdir()
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(served_root))
    monkeypatch.delenv("PAW_SITES_LOCAL_PORT", raising=False)

    workspace_id = str(ObjectId())
    user_id = str(ObjectId())
    source = _authored_source()

    # 1. CREATE — the real create_html_site handler persists an engine="html" pocket.
    with (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=workspace_id,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            return_value=user_id,
        ),
    ):
        created = await sites_create_mcp._create_html_site_handler(
            {"source": source, "name": "Bright Smile Dental"}
        )
    assert not created.get("is_error"), created
    pocket_id = json.loads(created["content"][0]["text"])["pocket_id"]

    doc = await _PocketDoc.get(ObjectId(pocket_id))
    assert doc is not None and doc.engine == "html" and doc.source == source

    # 2. PUBLISH — the real shared path: paw-sites generator (html, no Node build)
    #    -> local deploy. No fakes: this shells out to PAW_SITES_GEN_CMD.
    site = await sites_service.publish_pocket(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    assert site.deployed is True
    assert site.url and "127.0.0.1" in site.url

    # 3. SERVED ARTIFACT — assert on what actually landed in the deploy root, not on
    #    the generator's return value. deploy_local roots the tree at <served>/<site_id>/.
    site_id = str(site.id)
    served_dir = served_root / site_id
    served_index = served_dir / "index.html"
    assert served_index.is_file(), f"no served index.html under {served_dir}"
    assert served_index.read_text(encoding="utf-8") == source["index.html"], (
        "served page is not byte-identical to the authored index.html"
    )
    assert (served_dir / "styles.css").is_file()

    # 4. It is the HTML path — no SvelteKit scaffold / worker bundle in the served tree.
    assert not (served_dir / "package.json").exists()
    assert not (served_dir / ".svelte-kit").exists()
    assert not (served_dir / "_worker.js").exists()
