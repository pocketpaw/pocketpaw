# tests/ee/sites/test_pipeline_regression_gate.py
# Created: 2026-06-19 (P2c — the cross-repo E2E regression gate).
#
# THE PIPELINE REGRESSION GATE. This is the ONE consolidated end-to-end test that
# would have caught F1 + F2 + F4 TOGETHER — the three Paw Sites pipeline failures
# that each shipped because no single test exercised the real build → deploy_local
# → serve path AND the request-publish entry AND the build-path workerd reap in one
# place. Each prior failure had its own narrow unit test; this gate asserts the
# whole pipeline so a regression in any leg fails here, loudly, before it ships.
#
# It asserts, end-to-end on the REAL build+deploy_local path (the build itself is
# faked behind the GeneratorClient ``_runner`` seam — no real bun/node/workerd):
#   (a) F1 — arming a svelte pocket: the SERVED preview index.html (the DEPLOYED
#       file, NOT the source) contains BOTH ``data-paw-section`` AND
#       ``id="paw-edit-bridge"`` (the overlay contract the hover edit-pill needs).
#   (b) F2 — request-publish succeeds on a FRESH pocket AND on a LEGACY-shaped
#       pocket (zero artifact_versions). Both must SELF-HEAL a backfilled draft and
#       return a review Action, never 400.
#   (c) F4 — the build-path workerd nets ZERO across a build: the reaper fires once,
#       scoped to the build's project dir, after the static build. Reuses the exact
#       reap assertion from test_preview_serves_fresh_anchored_build.py rather than
#       duplicating it — it drives the REAL runner's build_static with a faked
#       subprocess and spies reap_build_workerd.
#
# Reuse, don't re-implement: the served-anchored-build fake runner comes from Agent
# A's test module (``_StaticBuildRunner``); the request-publish self-heal contract
# is the same one Agent B's test_request_publish.py pins; the reap leg reuses the
# generator_client reap wiring. This file CONSOLIDATES those into one gate.
from __future__ import annotations

from pathlib import Path

import pytest

# Reuse Agent A's fake runner (the one that drops a STALE anchorless build on
# ``generate`` and overwrites it with the FRESH ANCHORED build on the static-build
# step) so the served-file contract is asserted on the exact same mechanics, not a
# re-implementation.
from tests.ee.sites.test_preview_serves_fresh_anchored_build import (
    _FRESH_ANCHORED_HTML,
    _StaticBuildRunner,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _recording_bus():
    """Install a recording EventBus so the pockets service's ``emit`` calls don't
    raise (the real bus is only wired by ``init_realtime()`` at boot). Mirrors the
    fixture in test_preview_serves_fresh_anchored_build.py — autouse fixtures are
    module-scoped, so the gate needs its own copy."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


async def _make_svelte_pocket(name: str) -> str:
    """Create a real svelte pocket (zero artifact_versions — agent_create does not
    record a draft on create) and return its id. This is BOTH the fresh-pocket and
    the legacy-shaped case: a pocket with live content but no version rows."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws-gate",
        owner_id="u-gate",
        name=name,
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source={"src/routes/+page.svelte": "<h1>hi</h1>"},
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


async def test_sites_pipeline_regression_gate(beanie_test_db, tmp_path, monkeypatch):
    """THE PIPELINE REGRESSION GATE (P2c) — the consolidated F1+F2+F4 cover.

    One test, the whole pipeline. A regression in ANY of the three legs fails here.
    """
    from pocketpaw_ee.sites import local_server
    from pocketpaw_ee.sites import service as sites_service

    # Build + serve under throwaway dirs so the real home is never touched.
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path / "builds"))
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    # ---- (a) F1: arming a svelte pocket SERVES a fresh anchored+bridged build ----
    fresh_pocket = await _make_svelte_pocket("Gate Fresh")

    runner = _StaticBuildRunner()
    gen = sites_service.GeneratorClient(_runner=runner)

    # The EDITABLE preview / arm path (preview=True + builder_origin) — the real
    # make_site_editable / edit flow. REAL local deploy (persist + serve).
    site = await sites_service.publish_pocket(
        workspace_id="ws-gate",
        user_id="u-gate",
        pocket_id=fresh_pocket,
        preview=True,
        builder_origin="http://localhost:8888",
        _generator=gen,
    )

    served = local_server.sites_home() / f"preview-{fresh_pocket}" / "index.html"
    assert served.is_file(), f"the arm/preview path served no index.html at {served}"
    html = served.read_text()
    assert "data-paw-section" in html, (
        "F1 REGRESSION: the SERVED preview HTML has no section anchors — the hover "
        "edit-pill can never bind (a stale, anchorless build was served)"
    )
    assert 'id="paw-edit-bridge"' in html, (
        "F1 REGRESSION: the SERVED preview HTML has no edit-bridge — the iframe "
        "can't postMessage section rects (a stale build with no bridge was served)"
    )
    # It is the FRESH build the static-build step emitted, not the stale on-disk one.
    assert _FRESH_ANCHORED_HTML == html
    assert runner.static_build_count == 1, "the arm path must run `bun run build`"
    assert site.url.endswith(f"/preview-{fresh_pocket}/")
    # An arm/preview is NOT a live deploy → no deployed_at stamp leaks onto it (P2b).
    assert site.deployed is False
    assert site.deployed_at is None

    # ---- (b) F2: request-publish self-heals on FRESH and LEGACY-shaped pockets ----
    # A throwaway InstinctStore wired where both the sites service and the BP-3
    # executor read it, so request-publish can create the review Action.
    from pocketpaw.instinct.store import InstinctStore

    store = InstinctStore(tmp_path / "gate_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: store)

    from pocketpaw_ee.versions import service as versions

    # FRESH pocket: zero artifact_versions (agent_create records none). Submit for
    # review must BACKFILL a draft from the pocket's current content, not 400.
    assert await versions.get_draft(scope_type="pocket", scope_id=fresh_pocket) is None
    action_fresh = await sites_service.request_publish_pocket(
        workspace_id="ws-gate", user_id="u-gate", pocket_id=fresh_pocket
    )
    assert action_fresh is not None, "F2 REGRESSION: request-publish 400'd on a fresh pocket"
    healed_fresh = await versions.get_draft(scope_type="pocket", scope_id=fresh_pocket)
    assert healed_fresh is not None, "request-publish must self-heal a draft for a fresh pocket"
    assert action_fresh.parameters["_artifact_change"]["to_version_id"] == str(healed_fresh.id)

    # LEGACY-shaped pocket: same zero-versions shape (a site published before BP-1).
    # The self-heal path is identical — a second independent pocket proves it is not
    # the first call that happened to seed state.
    legacy_pocket = await _make_svelte_pocket("Gate Legacy")
    assert await versions.get_draft(scope_type="pocket", scope_id=legacy_pocket) is None
    action_legacy = await sites_service.request_publish_pocket(
        workspace_id="ws-gate", user_id="u-gate", pocket_id=legacy_pocket
    )
    assert action_legacy is not None, (
        "F2 REGRESSION: request-publish 400'd on a legacy-shaped pocket"
    )
    healed_legacy = await versions.get_draft(scope_type="pocket", scope_id=legacy_pocket)
    assert healed_legacy is not None, "request-publish must self-heal a legacy-shaped pocket"
    # First publish for each — nothing was ever published, so from is None.
    assert action_legacy.parameters["_artifact_change"]["from_version_id"] is None

    # ---- (c) F4: the build-path workerd nets ZERO across a build ----
    # Reuse the reap wiring assertion (the same contract Agent A's
    # test_build_reaps_workerd_after_static_build pins): drive the REAL runner's
    # build_static with a faked subprocess and spy the reaper — it must fire once,
    # scoped to THIS build's project dir, so no workerd is left behind (net zero).
    from pocketpaw_ee.sites import generator_client

    reaped: list[str] = []
    monkeypatch.setattr(
        generator_client,
        "reap_build_workerd",
        lambda project_dir: reaped.append(project_dir),
    )

    async def _fake_exec(*args, **kwargs):
        class _P:
            returncode = 0

            async def communicate(self):
                return b"built ok", b""

        return _P()

    monkeypatch.setattr(generator_client.asyncio, "create_subprocess_exec", _fake_exec)

    real_runner = generator_client._SubprocessRunner()
    build_dir = str(tmp_path / "reap-proj")
    Path(build_dir).mkdir(parents=True, exist_ok=True)
    ok, reason = await real_runner.build_static(build_dir, gate=True)

    assert ok, reason
    assert reaped == [build_dir], (
        "F4 REGRESSION: the build-path workerd was not reaped after the static "
        "build — orphaned workerd processes pile up and slow the box (net != zero)"
    )
