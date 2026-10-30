# tests/ee/sites/test_dev_bridge_source.py
# Created: 2026-06-26 (feat/sites-dev-bridge-source, S1 — the CRUX slice): prove the
# DEV-server-materialized SOURCE carries the gated edit-bridge so flipping
# BRIDGE_IN_DEV later won't regress the hover-edit overlay.
#
# THE GAP this covers: the dev-server HMR path (dev_server._default_materialize)
# calls GeneratorClient().build(static_build=False) to SKIP the prod `bun run build`
# (the speed win) and serve from SOURCE via `vite dev`. But it did NOT thread a
# ``builder_origin`` into the build, so the dev-served source had NO data-paw-section
# anchors and NO edit-bridge — flipping BRIDGE_IN_DEV without this is exactly the
# 2026-06-19 overlay regression that got backed out. The static /editable path DOES
# pass builder_origin (request Origin → PAW_SITES_BUILDER_ORIGIN fallback); S1
# mirrors that on the dev path: dev-preview endpoint → dev_preview_pocket →
# ensure_dev_server → _default_materialize → build(builder_origin=..., static_build=
# False). Only the generate/scaffold step needs the origin; the prod build still
# never runs on the dev path.
#
# TWO proofs, both driving the REAL _default_materialize → REAL GeneratorClient.build
# (the actual S1 arg-threading), differing only in the generator behind it:
#
#   1. test_dev_materialized_source_carries_bridge / _absent_without_origin
#      (REAL GENERATED SOURCE) — points PAW_SITES_GEN_CMD at the local paw-sites
#      generator that has the SE-1 gated injection (PR #14) and asserts the
#      materialized SOURCE FILES on disk: the composed section component's root has
#      data-paw-section="..." AND app.html (source) carries the gated edit-bridge IIFE
#      (id="paw-edit-bridge", the paw_edit flag, the origin). With no builder_origin
#      the gate holds — neither anchors nor bridge. SKIPPED when no such generator is
#      found (it needs node + the #14 build), so CI stays green without the toolchain.
#
#   2. test_dev_path_threads_builder_origin_into_build / _omits_when_unset
#      (ARG-THREADING, always runs) — drives _default_materialize through a real
#      GeneratorClient whose _runner is a FAKE that (a) RECORDS the siteConfig it is
#      called with (so we assert build() received builder_origin on the dev path) and
#      (b) writes anchored+bridged SOURCE to disk ONLY when siteConfig.builderOrigin
#      is set (mirroring the real generator's gate), so we still assert the
#      materialized source carries the bridge — without needing node/bun in CI. Also
#      pins static_build stays False (the dev speed win is preserved).
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio

# A minimal svelte pocket whose +page.svelte composes one section component (Hero),
# so the generator's stampSectionAnchors has a top-level section to anchor.
_HERO = '<section class="hero"><h1>Bright Smile</h1></section>\n'
_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte';</script>\n<Hero/>\n"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css';</script>\n<slot/>\n",
    "src/routes/+page.ts": "export const prerender = true;\n",
    "src/app.css": ":root{--brand:#0A84FF}\n",
    "src/lib/components/Hero.svelte": _HERO,
}

_BUILDER_ORIGIN = "http://localhost:8888"


def _pocket_wire() -> dict:
    """The pocket dict ``_default_materialize`` reads (engine/source/name/theme)."""
    return {
        "name": "Bright Smile",
        "engine": "svelte",
        "source": dict(_SVELTE_SOURCE),
        "rippleSpec": {"theme": {"primary": "#0A84FF"}},
    }


# --------------------------------------------------------------------------------
# Proof 1 — REAL GENERATED SOURCE (skipped when the #14 generator isn't available).
# --------------------------------------------------------------------------------


def _real_gen_cmd() -> list[str] | None:
    """Locate a paw-sites generator that has the SE-1 gated edit-bridge injection
    (PR #14). Prefer an explicit PAW_SITES_GEN_CMD; else probe the known local
    checkouts' built ``dist/cli.js``. Returns the argv to run, or None if none has
    the #14 injection (then the real-source proof skips and proof 2 still runs)."""
    explicit = os.environ.get("PAW_SITES_DEV_BRIDGE_GEN_CMD") or os.environ.get("PAW_SITES_GEN_CMD")
    if explicit:
        import shlex

        argv = shlex.split(explicit)
        if argv and (shutil.which(argv[0]) or Path(argv[0]).exists()):
            return argv

    node = shutil.which("node")
    if not node:
        return None
    # Known local checkouts that build the generator. The SE-1 injection (PR #14)
    # lives in edit-bridge.{ts,js}; only a dist that carries the gated injection
    # produces the bridged source, so we require the marker token in the build.
    candidates = [
        Path.home() / "Documents/paw-worktrees/intg-paw-sites/dist/cli.js",
        Path.home() / "Documents/paw-workspace/paw-sites/dist/cli.js",
    ]
    for cli in candidates:
        if not cli.is_file():
            continue
        dist = cli.parent
        injects = any(
            (dist / name).is_file()
            and "data-paw-section" in (dist / name).read_text(errors="ignore")
            and "builderOrigin" in (dist / name).read_text(errors="ignore")
            for name in ("index.js", "svelte-scaffold.js", "edit-bridge.js")
        )
        if injects:
            return [node, str(cli)]
    return None


_REAL_GEN = _real_gen_cmd()
_skip_no_gen = pytest.mark.skipif(
    _REAL_GEN is None,
    reason="no paw-sites generator with the SE-1 (#14) gated edit-bridge injection found",
)


async def _materialize_real(*, builder_origin: str | None, tmp_path: Path, monkeypatch) -> Path:
    """Run the REAL dev-path materialize against the real generator and return the
    materialized project dir. Threads through the actual S1 code path:
    _default_materialize → GeneratorClient().build(builder_origin=..., static_build=
    False). bun install / static build never run (static_build=False), so this needs
    only ``node`` + the generator — no bun/workerd."""
    from pocketpaw_ee.sites import dev_server

    monkeypatch.setenv("PAW_SITES_GEN_CMD", " ".join(_REAL_GEN))  # type: ignore[arg-type]
    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path / "builds"))

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=_pocket_wire()),
    ):
        project_dir = await dev_server._default_materialize(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            builder_origin=builder_origin,
        )
    return Path(project_dir)


@_skip_no_gen
async def test_dev_materialized_source_carries_bridge(tmp_path, monkeypatch):
    """S1 CRUX (real source): the dev-materialized SOURCE — with a builder_origin —
    carries the bridge. The composed section component's root has data-paw-section,
    and app.html (SOURCE, not build output) contains the gated edit-bridge IIFE. This
    is exactly what the hover-edit overlay needs once BRIDGE_IN_DEV is flipped."""
    project_dir = await _materialize_real(
        builder_origin=_BUILDER_ORIGIN, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    hero = project_dir / "src/lib/components/Hero.svelte"
    assert hero.is_file(), f"no Hero component at {hero}"
    hero_src = hero.read_text()
    assert "data-paw-section" in hero_src, (
        "the composed section component's SOURCE root has no data-paw-section anchor "
        "— the hover-edit overlay can't bind a section"
    )

    app_html = project_dir / "src/app.html"
    assert app_html.is_file(), f"no app.html source at {app_html}"
    bridge_src = app_html.read_text()
    assert 'id="paw-edit-bridge"' in bridge_src, (
        "app.html SOURCE has no gated edit-bridge IIFE — the dev-served page can't "
        "postMessage section rects"
    )
    # The IIFE references the paw_edit gate flag and the builder origin.
    assert "paw_edit" in bridge_src
    assert _BUILDER_ORIGIN in bridge_src


@_skip_no_gen
async def test_dev_materialized_source_no_bridge_without_origin(tmp_path, monkeypatch):
    """The gate holds (real source): with NO builder_origin the dev-materialized
    SOURCE carries neither anchors nor bridge — the generator injects only when the
    origin is set, so the dev source stays non-editable (matches the publish path's
    non-editable behaviour)."""
    project_dir = await _materialize_real(
        builder_origin=None, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    hero_src = (project_dir / "src/lib/components/Hero.svelte").read_text()
    assert "data-paw-section" not in hero_src, "anchor leaked with no builder_origin"

    app_html = project_dir / "src/app.html"
    if app_html.is_file():
        assert 'id="paw-edit-bridge"' not in app_html.read_text(), (
            "edit-bridge leaked into app.html with no builder_origin — the gate broke"
        )


# --------------------------------------------------------------------------------
# Proof 2 — ARG-THREADING via a fake runner (always runs in CI; no node/bun needed).
# --------------------------------------------------------------------------------

# The exact source-injection tokens the real SE-1 generator emits (mirrored so the
# fake's gated output matches what the overlay looks for).
_FAKE_ANCHORED_HERO = (
    '<section data-paw-section="Hero" class="hero"><h1>Bright Smile</h1></section>\n'
)


def _fake_bridge_app_html(origin: str) -> str:
    return (
        "<!doctype html><html><head>%sveltekit.head%</head>"
        "<body><div>%sveltekit.body%</div>"
        f'<script id="paw-edit-bridge">(function(){{'
        f"var p=new URLSearchParams(location.search);"
        f"if(p.get('paw_edit')!=='1')return;"
        f"window.parent.postMessage({{}}, {json.dumps(origin)});"
        f"}})();</script></body></html>"
    )


class _GatedFakeRunner:
    """A fake GeneratorClient runner that mirrors the real generator's CONTRACT for
    this test: it RECORDS the siteConfig it was called with (so we assert build()
    forwarded builder_origin on the dev path), and it materializes svelte SOURCE to
    disk, injecting the data-paw-section anchor + the gated edit-bridge into app.html
    ONLY when ``siteConfig.builderOrigin`` is present — exactly the SE-1 gate. No
    install/build_static here: the dev path passes static_build=False, so those never
    run (which this test also pins)."""

    def __init__(self) -> None:
        self.generate_site_config: dict | None = None
        self.install_called = False
        self.build_static_called = False

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        site_config = input_json.get("siteConfig", {})
        self.generate_site_config = site_config
        origin = site_config.get("builderOrigin")
        src = input_json.get("source", {})

        proj = Path(out_dir)
        proj.mkdir(parents=True, exist_ok=True)
        # Write the svelte source map to disk (what materializeSource does).
        for rel, contents in src.items():
            p = proj / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contents)
        # The generator always materializes app.html; the gated injection only fires
        # when builderOrigin is set.
        app_html = proj / "src/app.html"
        app_html.parent.mkdir(parents=True, exist_ok=True)
        if origin:
            # Stamp the composed section root (mirrors stampSectionAnchors).
            (proj / "src/lib/components/Hero.svelte").write_text(_FAKE_ANCHORED_HERO)
            app_html.write_text(_fake_bridge_app_html(origin))
        else:
            app_html.write_text("<!doctype html><html><head></head><body></body></html>")
        return {"projectDir": str(proj), "engine": "svelte"}

    def install_inputs_hash(self, project_dir: str) -> str:
        return "h1"

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.install_called = True
        return True, "ok"

    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]:
        # The dev path must NEVER call this (static_build=False).
        self.build_static_called = True
        return True, "ok"


async def _materialize_with_fake_runner(
    *, builder_origin: str | None, tmp_path: Path, monkeypatch
) -> tuple[Path, _GatedFakeRunner]:
    """Drive the REAL _default_materialize, but with the GeneratorClient constructed
    with a fake runner, so the actual S1 arg-threading (materialize → build) runs
    end-to-end without node/bun. We inject the fake runner by patching the
    GeneratorClient the materialize constructs."""
    from pocketpaw_ee.sites import dev_server, generator_client

    monkeypatch.setenv("PAW_SITES_BUILD_DIR", str(tmp_path / "builds"))
    runner = _GatedFakeRunner()

    real_client_cls = generator_client.GeneratorClient

    def _client_factory(*_args, **_kwargs) -> generator_client.GeneratorClient:
        # _default_materialize calls ``GeneratorClient()`` (no runner) — return one
        # backed by our gated fake so the real build() arg-threading runs without bun.
        return real_client_cls(_runner=runner)

    # _default_materialize imports GeneratorClient from generator_client at call time
    # (``from ... import GeneratorClient``), so patch it on the source module.
    with (
        patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=_pocket_wire()),
        ),
        patch.object(generator_client, "GeneratorClient", _client_factory),
    ):
        project_dir = await dev_server._default_materialize(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            builder_origin=builder_origin,
        )
    return Path(project_dir), runner


async def test_dev_path_threads_builder_origin_into_build(tmp_path, monkeypatch):
    """S1 arg-threading: the dev path forwards builder_origin all the way into the
    generate step (build() puts it on siteConfig.builderOrigin), and keeps
    static_build=False (no prod build — the dev speed win). The materialized SOURCE
    then carries the bridge."""
    project_dir, runner = await _materialize_with_fake_runner(
        builder_origin=_BUILDER_ORIGIN, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    # build() received the origin and put it on the generator input's siteConfig.
    assert runner.generate_site_config is not None
    assert runner.generate_site_config.get("builderOrigin") == _BUILDER_ORIGIN
    # The dev path NEVER runs the prod static build (the whole point — vite dev only).
    assert runner.build_static_called is False, "the dev path must not run `bun run build`"

    # And the materialized SOURCE carries the bridge (anchors + gated IIFE).
    hero_src = (project_dir / "src/lib/components/Hero.svelte").read_text()
    assert "data-paw-section" in hero_src
    bridge_src = (project_dir / "src/app.html").read_text()
    assert 'id="paw-edit-bridge"' in bridge_src
    assert "paw_edit" in bridge_src
    assert _BUILDER_ORIGIN in bridge_src


async def test_dev_path_omits_origin_when_unset(tmp_path, monkeypatch):
    """The gate holds end-to-end: with no builder_origin the generator input carries
    no builderOrigin and the materialized SOURCE has neither anchors nor bridge."""
    project_dir, runner = await _materialize_with_fake_runner(
        builder_origin=None, tmp_path=tmp_path, monkeypatch=monkeypatch
    )

    assert runner.generate_site_config is not None
    # build() omits builderOrigin from siteConfig when the origin is None/empty.
    assert "builderOrigin" not in runner.generate_site_config
    assert runner.build_static_called is False

    hero_src = (project_dir / "src/lib/components/Hero.svelte").read_text()
    assert "data-paw-section" not in hero_src
    bridge_src = (project_dir / "src/app.html").read_text()
    assert 'id="paw-edit-bridge"' not in bridge_src
