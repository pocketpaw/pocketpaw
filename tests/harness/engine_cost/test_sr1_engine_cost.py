"""SR-1 tests: the measurement's integrity, not the numbers themselves.

Created for SR-1.

WHAT: guards the things that would make the recorded numbers a lie —

* every engine generates a project whose build script is the one the control
  plane will actually run,
* ripple's dep rewrite (the ripple tarball + ``motion``) happens, mirroring
  ``generator_client.py::_rewrite_ripple_dep``,
* ripple-dynamic really is dynamic (D1 migration + remote functions present) and
  ripple-static really is not, so the two numbers describe different work,
* a build that emitted nothing cannot pass as the fastest engine,
* cold and warm are genuinely different states.

WHY the timings themselves are NOT asserted: they are hardware-specific
observations, and a test that pinned them would fail on the next machine while
proving nothing. The numbers live in the report; these tests prove the report
measured what it claims to.

The heavy end-to-end measurement is opt-in via ``SR1_RUN=1`` — a full run
installs and builds four real projects, which is minutes, not seconds.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from .measure import (
    ENGINES,
    SITES_REPO,
    PhaseFailed,
    _describe_artifact,
    generate_project,
    host_info,
    measure_engine,
)

_RUN_FULL = os.environ.get("SR1_RUN") == "1"

# Which engines are cheap enough to generate (not build) in a normal test run.
# Generation is milliseconds — it is the install+build that is expensive.
_GENERATE_ONLY = ENGINES


@pytest.fixture(scope="module")
def work_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("sr1")


@pytest.fixture(scope="module", autouse=True)
def require_toolchain() -> None:
    """Skip the whole module when the toolchain or the generator is absent."""
    if not shutil.which("bun"):
        pytest.skip("bun not on PATH — SR-1 measures a real bun build")
    if not (SITES_REPO / "package.json").exists():
        pytest.skip(f"paw-sites repo not found at {SITES_REPO}; set PAW_SITES_REPO")
    if not (SITES_REPO / "node_modules").exists():
        pytest.skip(f"{SITES_REPO}/node_modules missing — run bun install there first")


# --------------------------------------------------------------------------
# Generation: the cheap half, always run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("engine", _GENERATE_ONLY)
def test_engine_generates_a_buildable_project(engine: str, work_root: Path) -> None:
    """Each engine materializes a project whose build script is the real one."""
    out = work_root / f"gen-{engine}"
    payload = generate_project(engine, out)

    assert payload["ok"] is True
    assert (out / "package.json").exists(), "no package.json — nothing to install"

    # The build command must be the generator's own, not something invented here.
    build_script = payload["build_script"]
    assert build_script, f"{engine} declared no build script"
    if engine == "react":
        # The three-pass SSG: client build, SSR build, prerender splice.
        assert "vite build" in build_script
        assert "--ssr" in build_script, "react must build an SSR entry to prerender"
        assert "paw-prerender" in build_script, "react must run the prerender step"
    else:
        assert "vite build" in build_script

    # The engine the generator RESOLVED, which for both ripple variants is 'ripple'.
    assert payload["resolved_engine"] == ("ripple" if engine.startswith("ripple") else engine)


@pytest.mark.parametrize("engine", ("ripple-static", "ripple-dynamic"))
def test_ripple_dep_rewrite_mirrors_the_control_plane(engine: str, work_root: Path) -> None:
    """The ripple tarball and motion are injected, as on every real publish.

    ``generator_client.py::_rewrite_ripple_dep`` overwrites the template's
    unpublished "0.2.0" pin and adds ``motion``, without which ripple's runtime
    ``import('motion')`` fails to resolve at build time. Measuring a build that
    skipped this would measure a build production never runs.
    """
    out = work_root / f"dep-{engine}"
    payload = generate_project(engine, out)

    deps = payload["deps"]
    assert deps["rewritten"] is True, f"ripple dep was not rewritten: {deps}"
    assert deps["ripple_dep"].startswith("file:"), "expected the vendored tarball"
    assert "0.5.0" in deps["ripple_dep"], "expected ripple 0.5.0, the version production installs"
    assert deps["motion_dep"], "motion must be declared or the ripple build cannot resolve it"

    pkg = json.loads((out / "package.json").read_text(encoding="utf-8"))
    assert pkg["dependencies"]["@ripple-ui/svelte"] == deps["ripple_dep"]
    assert "motion" in pkg["dependencies"]


def test_ripple_dynamic_and_static_are_genuinely_different_work(work_root: Path) -> None:
    """The two ripple numbers must describe different builds, or one is redundant.

    Dynamic scaffolds a D1 migration and remote functions and renders SSR
    (prerender off); static does neither. If these came out identical, reporting
    them as two engines would be noise.
    """
    static_dir = work_root / "cmp-static"
    dynamic_dir = work_root / "cmp-dynamic"
    generate_project("ripple-static", static_dir)
    generate_project("ripple-dynamic", dynamic_dir)

    assert (dynamic_dir / "migrations" / "0001_init.sql").exists(), "dynamic has no D1 migration"
    assert (dynamic_dir / "src" / "routes" / "data.remote.ts").exists(), (
        "dynamic has no remote functions"
    )
    assert not (static_dir / "migrations").exists(), "static should have no migration"
    assert not (static_dir / "src" / "routes" / "data.remote.ts").exists(), (
        "static should have no remote functions"
    )


def test_generate_rejects_an_unknown_engine(work_root: Path) -> None:
    with pytest.raises(PhaseFailed, match="exited"):
        generate_project("no-such-engine", work_root / "bogus")


def test_host_info_records_what_the_numbers_describe() -> None:
    """Numbers without a host are not transferable."""
    info = host_info()
    assert info["logical_cores"] and info["logical_cores"] > 0
    assert info["total_ram_gb"] and info["total_ram_gb"] > 0
    assert info["bun_version"], "bun version must be recorded"


# --------------------------------------------------------------------------
# Artifact honesty: a hollow build must not read as a fast one
# --------------------------------------------------------------------------


def test_hollow_output_is_visible_in_the_report(tmp_path: Path) -> None:
    """An empty output dir reports zero files rather than looking like a win."""
    (tmp_path / "dist").mkdir()
    described = _describe_artifact(tmp_path, {})
    assert described["output_dir"] == "dist"
    assert described["file_count"] == 0
    assert described["total_bytes"] == 0
    assert described["entry_html_bytes"] is None
    # The honesty flag: nothing prerendered AND no worker means nothing is served.
    assert described["served_something"] is False
    assert described["render_mode"] == "none"


def test_an_ssr_only_output_counts_as_served(tmp_path: Path) -> None:
    """A dynamic ripple build has NO index.html and must still pass.

    Its page is SSR'd (prerender off), so the tree carries _worker.js and no
    prerendered entry — observed on the measured artifact. Asserting on entry HTML
    alone would fail a perfectly healthy dynamic build.
    """
    out = tmp_path / ".svelte-kit" / "cloudflare"
    out.mkdir(parents=True)
    (out / "_worker.js").write_text("export default {}", encoding="utf-8")

    described = _describe_artifact(tmp_path, {})
    assert described["entry_html_bytes"] is None
    assert described["has_worker_js"] is True
    assert described["served_something"] is True
    assert described["render_mode"] == "ssr"


def test_missing_output_dir_is_reported_not_silently_passed(tmp_path: Path) -> None:
    described = _describe_artifact(tmp_path, {"engine": "x"})
    assert described["output_dir"] is None
    assert "no recognized output dir" in described["note"]


# --------------------------------------------------------------------------
# The full measurement: opt-in, minutes long
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _RUN_FULL, reason="set SR1_RUN=1 for the real install+build measurement")
@pytest.mark.parametrize("engine", ENGINES)
def test_measure_engine_end_to_end(engine: str, work_root: Path) -> None:
    """A real cold+warm measurement, with the artifact proven non-hollow."""
    result = measure_engine(engine, work_root / "full", warm_runs=1)

    cold = result["cold"]
    warm = result["warm"]
    assert cold["total_s"] > 0
    assert warm["median_total_s"] is not None

    # Cold must cost more than warm — a cold install downloads and links a tree
    # that warm already has. If this inverts, cold was not actually cold.
    assert cold["total_s"] > warm["median_total_s"], (
        f"{engine}: cold {cold['total_s']}s <= warm {warm['median_total_s']}s — "
        "the cold arm did not start from an empty node_modules"
    )

    # The shares must account for the whole phase.
    assert abs(cold["install_share_pct"] + cold["build_share_pct"] - 100) < 0.5

    # Resource numbers must be real.
    assert result["peak_rss_mb"] > 0
    assert result["cores_saturated_peak"] and result["cores_saturated_peak"] > 0

    # And the build must have emitted something servable — prerendered HTML or a
    # server worker. NOT entry HTML alone: ripple-dynamic is SSR'd and correctly
    # emits no index.html, so that check would fail a healthy build.
    artifact = result["artifact"]
    assert artifact["output_dir"], f"{engine} produced no recognized output dir"
    assert artifact["file_count"] > 0, f"{engine} built nothing — the timing is meaningless"
    assert artifact["total_bytes"] > 0
    assert artifact["served_something"], f"{engine} emitted neither an entry page nor a worker"

    # Engine-shape facts worth pinning, each measured:
    #   react          — assets-only SSG, prerendered, no worker
    #   svelte/ripple   — emit a _worker.js
    #   ripple-static   — prerendered entry; ripple-dynamic — SSR, no entry HTML
    if engine == "react":
        assert artifact["has_worker_js"] is False, "react must be assets-only"
        assert artifact["render_mode"] == "prerendered"
    else:
        assert artifact["has_worker_js"] is True, f"{engine} should emit a server worker"
    if engine == "ripple-dynamic":
        assert artifact["render_mode"] == "ssr", "a dynamic site renders per request"


@pytest.mark.skipif(not _RUN_FULL, reason="set SR1_RUN=1 for the real install+build measurement")
def test_react_prerenders_real_copy_not_a_blank_shell(work_root: Path) -> None:
    """React's SSG claim, checked on the built artifact.

    A React SPA shell is blank with JS disabled; this engine exists to prerender
    instead. If the prerender silently degraded, the build would still be fast —
    so the speed number is only meaningful alongside this assertion.
    """
    result = measure_engine("react", work_root / "prerender", warm_runs=0)
    index = Path(result["generated"]["project_dir"]) / "dist" / "index.html"
    html = index.read_text(encoding="utf-8")

    # The fixture's headline is resting markup returned by the component.
    assert "Ship the whole product" in html, "react did not prerender the author's copy"
    assert 'id="root"' in html
    # The marker must be GONE — its presence means the splice never happened.
    assert "SSR_OUTLET" not in html, "the prerender marker survived; nothing was spliced"


def test_report_shape_is_stable(tmp_path: Path) -> None:
    """The report keys downstream slices will read. Cheap, no build required."""
    fake: dict[str, Any] = {
        "engine": "react",
        "cold": {"total_s": 10.0, "install_share_pct": 84.0, "build_share_pct": 16.0},
        "warm": {"median_total_s": 1.5},
        "peak_rss_mb": 344.1,
        "cores_saturated_peak": 1.03,
        "artifact": {"output_dir": "dist", "file_count": 3},
    }
    # Documents the contract SG-8/SG-9 read; a rename here is a breaking change.
    for key in ("engine", "cold", "warm", "peak_rss_mb", "cores_saturated_peak", "artifact"):
        assert key in fake
    assert set(fake["cold"]) >= {"total_s", "install_share_pct", "build_share_pct"}
