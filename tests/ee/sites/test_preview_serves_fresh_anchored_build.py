# tests/ee/sites/test_preview_serves_fresh_anchored_build.py
# Created: 2026-06-19 (fix/sites-preview-fresh-build, P0a + P1a) — reproduce-first
# cover for the #1 Paw Sites bug: the hover edit-pill never appears because the
# editable PREVIEW build SERVED STALE HTML without the data-paw-section anchors.
#
# ROOT CAUSE (PERF-4 overloaded the ``smoke`` flag): the ``smoke`` flag gated
# ``_runner.smoke()``, the ONLY step that ran ``bun run build`` — the step that
# emits the deployable ``.svelte-kit/cloudflare/`` static output (with the section
# anchors + the injected ``id="paw-edit-bridge"``). ``service.publish()`` passes
# ``smoke = not preview``, so the preview/editable path re-stamped the SOURCE with
# anchors but SKIPPED ``bun run build``; ``persist_site`` then copied whatever the
# LAST build left on disk — the stale, non-anchored LIVE build. So the SERVED
# preview had 0 anchors → no pill.
#
# These tests assert on the SERVED (deployed) ``index.html`` — NOT the source — so
# they prove the file the iframe actually loads carries the anchors + bridge:
#   * test_preview_serves_fresh_anchored_build — FAILS on the pre-fix code (the
#     served file is the stale non-anchored build) and PASSES after the fix
#     (``bun run build`` always runs on the preview/local-served path, so the
#     served file is the FRESH anchored build).
#   * test_build_reaps_workerd_after_static_build — P1a: the build-path workerd
#     leak is reaped after each ``bun run build`` (the reap function is invoked).
#
# The build is faked behind the GeneratorClient ``_runner`` seam: ``generate``
# writes a STALE (anchorless) ``.svelte-kit/cloudflare/index.html`` to simulate a
# prior LIVE build sitting on disk; the static-build step (``bun run build``)
# overwrites it with the FRESH ANCHORED build. No real bun/node/workerd spawns.
from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.sites import local_server

pytestmark = pytest.mark.asyncio

# What the prior LIVE build left on disk: NO anchors, NO bridge — the stale HTML
# the bug serves on a preview.
_STALE_HTML = "<html><body><h1>old live build</h1></body></html>"
# What a FRESH `bun run build` of an EDITABLE site emits: section anchors + the
# injected edit-bridge. The exact tokens the hover edit-pill overlay needs.
_FRESH_ANCHORED_HTML = (
    '<html><head><script id="paw-edit-bridge">/* bridge */</script></head>'
    '<body><section data-paw-section="hero"><h1>fresh editable build</h1></section>'
    "</body></html>"
)

_CLOUDFLARE_BUILD_REL = ".svelte-kit/cloudflare"


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a recording EventBus so the pockets service's ``emit`` calls don't
    raise (the real bus is only wired by ``init_realtime()`` at boot)."""
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


class _StaticBuildRunner:
    """Fake runner that models the on-disk build output: ``generate`` drops a
    STALE anchorless build (a prior live build), and the static-build step
    (``build_static`` — the real runner's ``bun run build``) overwrites it with
    the FRESH ANCHORED build. This is how the SERVED file ends up stale when the
    static build is skipped on the preview path (the bug)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.static_build_count = 0

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        # Simulate a stale prior LIVE build already on disk (anchorless HTML).
        cf = Path(out_dir, _CLOUDFLARE_BUILD_REL)
        cf.mkdir(parents=True, exist_ok=True)
        (cf / "index.html").write_text(_STALE_HTML)
        return {"projectDir": out_dir, "rippleVersion": "0.2.0"}

    def install_inputs_hash(self, project_dir: str) -> str:
        return "h1"

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]:
        """The static-output step (== the real runner's ``bun run build``). Always
        emits the FRESH ANCHORED build; ``gate`` toggles only the SSR fail-check."""
        self.calls.append("build_static")
        self.static_build_count += 1
        cf = Path(project_dir, _CLOUDFLARE_BUILD_REL)
        cf.mkdir(parents=True, exist_ok=True)
        (cf / "index.html").write_text(_FRESH_ANCHORED_HTML)
        return True, "ok"


async def test_preview_serves_fresh_anchored_build(beanie_test_db, tmp_path, monkeypatch):
    """The #1 bug, reproduce-first: the SERVED preview index.html (the DEPLOYED
    file, not the source) must contain BOTH ``data-paw-section`` AND
    ``id="paw-edit-bridge"``. On the pre-fix code the preview path skipped
    ``bun run build`` and ``persist_site`` copied the stale anchorless build, so
    this FAILS; with the fix the static build always runs on the preview path so
    the served file is fresh + anchored."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import service as sites_service

    # Build + serve under throwaway dirs so the real home is never touched.
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path / "builds"))
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    # A real svelte pocket so publish_pocket reads engine/source from the wire.
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source={"src/routes/+page.svelte": "<h1>hi</h1>"},
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None

    runner = _StaticBuildRunner()
    gen = sites_service.GeneratorClient(_runner=runner)

    # The EDITABLE preview path (preview=True + builder_origin) — exactly the
    # make_site_editable / edit flow. Real local deploy (persist + serve).
    site = await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        preview=True,
        builder_origin="http://localhost:8888",
        _generator=gen,
    )

    # Read the SERVED (deployed) file — the one the iframe loads — NOT the source.
    served = local_server.sites_home() / f"preview-{pocket_id}" / "index.html"
    assert served.is_file(), f"no served preview index.html at {served}"
    html = served.read_text()

    assert "data-paw-section" in html, (
        "the SERVED preview HTML has no section anchors — the hover edit-pill can "
        "never bind (the bug: a stale, anchorless build was served)"
    )
    assert 'id="paw-edit-bridge"' in html, (
        "the SERVED preview HTML has no edit-bridge — the iframe can't postMessage "
        "section rects (the bug: a stale build with no injected bridge was served)"
    )
    # And it is the FRESH build, not the stale one left on disk by `generate`.
    assert "fresh editable build" in html
    assert "old live build" not in html
    # The static build (`bun run build`) actually ran on the preview path.
    assert runner.static_build_count == 1
    # The site URL points at the stable preview path the served file lives under.
    assert site.url.endswith(f"/preview-{pocket_id}/")


async def test_build_reaps_workerd_after_static_build(tmp_path, monkeypatch):
    """P1a: each static build (``bun run build``) reaps the prerender-spawned
    workerd so they don't pile up. Asserted by spying the reap function — it is
    invoked once per static build, scoped to the build's project dir."""
    from pocketpaw_ee.sites import generator_client

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))

    reaped: list[str] = []
    monkeypatch.setattr(
        generator_client,
        "reap_build_workerd",
        lambda project_dir: reaped.append(project_dir),
    )

    runner = generator_client._SubprocessRunner()

    # Drive only the static-build step with a fake `bun run build` that succeeds
    # without spawning a real process, so we isolate the reap-after-build wiring.
    async def _fake_exec(*args, **kwargs):
        class _P:
            returncode = 0

            async def communicate(self):
                return b"built ok", b""

        return _P()

    monkeypatch.setattr(generator_client.asyncio, "create_subprocess_exec", _fake_exec)

    project_dir = str(tmp_path / "proj")
    Path(project_dir).mkdir(parents=True, exist_ok=True)
    ok, reason = await runner.build_static(project_dir, gate=True)

    assert ok, reason
    # The reaper ran exactly once, scoped to THIS build's project dir.
    assert reaped == [project_dir]


# --- the split semantics: bun run build vs the SSR fail-gate --------------------


class _SplitCountingRunner:
    """A runner with ``build_static`` that records the call sequence + the ``gate``
    flag, so the split (always-build vs gate-only-on-publish) can be asserted
    without bun/workerd. ``ssr_fail`` makes the build report an SSR marker."""

    def __init__(self, *, ssr_fail: bool = False) -> None:
        self.calls: list[str] = []
        self.build_static_count = 0
        self.last_gate: bool | None = None
        self.ssr_fail = ssr_fail

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        return {"projectDir": out_dir, "rippleVersion": "0.2.0"}

    def install_inputs_hash(self, project_dir: str) -> str:
        return "h1"

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]:
        self.calls.append("build_static")
        self.build_static_count += 1
        self.last_gate = gate
        if self.ssr_fail and gate:
            return False, "workerd SSR failure: window is not defined"
        return True, "ok"


def _kwargs(tmp_path):
    return dict(
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        site_id="site_1",
        title="Bright Smile",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
        pocket_id="pocket_abc",
    )


async def test_preview_build_still_runs_static_build_gate_off(tmp_path, monkeypatch):
    """P0a core: a PREVIEW build (smoke=False) STILL runs `bun run build`
    (build_static) — only the SSR fail-gate is off. Before the fix smoke=False
    skipped the build entirely, which served the stale anchorless output."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _SplitCountingRunner()
    client = GeneratorClient(_runner=runner)

    await client.build(**_kwargs(tmp_path), smoke=False)

    assert runner.build_static_count == 1, "the preview path must still BUILD"
    assert runner.last_gate is False, "the SSR fail-gate is OFF on a preview build"
    assert runner.calls == ["generate", "install", "build_static"]


async def test_publish_build_runs_static_build_with_gate_on(tmp_path, monkeypatch):
    """A LIVE publish (smoke=True default) builds AND turns the SSR fail-gate ON."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _SplitCountingRunner()
    client = GeneratorClient(_runner=runner)

    await client.build(**_kwargs(tmp_path))  # smoke defaults to True

    assert runner.build_static_count == 1
    assert runner.last_gate is True, "the SSR fail-gate is ON for a live publish"


async def test_publish_still_gates_on_ssr_failure(tmp_path, monkeypatch):
    """The split keeps the publish gate: a live build whose SSR render fails still
    raises SmokeGateFailed."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient, SmokeGateFailed

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _SplitCountingRunner(ssr_fail=True)
    client = GeneratorClient(_runner=runner)

    with pytest.raises(SmokeGateFailed) as exc:
        await client.build(**_kwargs(tmp_path), smoke=True)
    assert "window is not defined" in str(exc.value)


async def test_preview_not_blocked_by_would_fail_ssr(tmp_path, monkeypatch):
    """A preview build (smoke=False) is NOT blocked by a would-fail SSR render — it
    builds + serves fresh; the live publish still gates + rolls back."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _SplitCountingRunner(ssr_fail=True)
    client = GeneratorClient(_runner=runner)

    # No raise — the preview builds despite a would-fail SSR render.
    result = await client.build(**_kwargs(tmp_path), smoke=False)
    assert result.project_dir == str(tmp_path / "pocket_abc")
    assert runner.build_static_count == 1


async def test_static_build_false_skips_build(tmp_path, monkeypatch):
    """The dev-server path (static_build=False, serves from `vite dev`) skips the
    static build entirely — only generate + install run."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path))
    runner = _SplitCountingRunner()
    client = GeneratorClient(_runner=runner)

    await client.build(**_kwargs(tmp_path), smoke=False, static_build=False)

    assert runner.build_static_count == 0, "the dev path must NOT run `bun run build`"
    assert runner.calls == ["generate", "install"]


async def test_deploy_local_fails_soft_on_missing_build(tmp_path, monkeypatch):
    """P1a: deploy_local returns the PRIOR deploy's URL (not a 500) when a fresh
    build produced no static output but a prior deploy is on disk; with no prior
    deploy it raises a clear MissingBuildOutput instead of a bare FileNotFoundError."""
    from pocketpaw_ee.sites import local_server

    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))

    # No build output AND no prior deploy → clear typed error (not a bare 500).
    empty_proj = tmp_path / "empty-proj"
    empty_proj.mkdir()
    with pytest.raises(local_server.MissingBuildOutput):
        local_server.deploy_local("site_x", str(empty_proj))

    # Seed a prior deploy, then deploy a project with NO fresh build → fail soft to
    # the prior URL.
    prior = local_server.sites_home() / "site_x"
    prior.mkdir(parents=True, exist_ok=True)
    (prior / "index.html").write_text("<html>prior good build</html>")
    url = local_server.deploy_local("site_x", str(empty_proj))
    assert url.endswith("/site_x/")
