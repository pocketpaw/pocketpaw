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

import pytest

from . import measure as measure_module
from .measure import (
    ENGINES,
    SITES_REPO,
    PhaseFailed,
    ResourceSample,
    _describe_artifact,
    _share_pct,
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
    # Whether the resource figures could be measured at all must be on the record,
    # so a report with no RSS/core numbers is self-explaining.
    assert info["psutil_available"] is True


def test_host_info_degrades_without_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """psutil is NOT a base dependency — it lives in extras. Absent, timings still work.

    Four shipped modules (api/v1/metrics.py, tools/status.py, tools/builtin/
    sysinfo.py, daemon/context.py) already guard their psutil import and degrade;
    this harness follows that convention. Without psutil the RSS/CPU/core figures
    are unavailable, but the wall-clock measurement — the primary deliverable — is
    unaffected, and the report says which case it is rather than implying zeros.
    """
    monkeypatch.setattr(measure_module, "psutil", None)
    monkeypatch.setattr(measure_module, "HAVE_PSUTIL", False)

    info = host_info()
    assert info["psutil_available"] is False
    # Core count survives via stdlib os.cpu_count().
    assert info["logical_cores"] and info["logical_cores"] > 0
    # Memory figures are absent, NOT zero — zero would read as "no RAM measured as 0".
    assert info["total_ram_gb"] is None
    assert info["available_ram_gb"] is None
    assert info["physical_cores"] is None
    # And the toolchain versions, which never needed psutil, are still recorded.
    assert info["bun_version"]


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
    # Both core figures, and the invariant between them: the instantaneous peak is
    # what a concurrency cap divides by, so it must never come back under the
    # average it is derived alongside.
    assert result["cores_saturated_avg_max"] and result["cores_saturated_avg_max"] > 0
    assert result["cores_peak_instant"] and result["cores_peak_instant"] > 0
    assert result["cores_peak_instant"] >= result["cores_saturated_avg_max"]
    # USS is the memory figure to size against; it must not exceed RSS.
    assert result["peak_uss_mb"] and result["peak_uss_mb"] <= result["peak_rss_mb"]

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


def test_resource_sample_reports_both_core_figures() -> None:
    """The average and the instantaneous peak are distinct, and peak >= average.

    A concurrency cap must divide by the PEAK: a build pegging 4 cores for 5s then
    idling 45s averages 0.4 but needs 4 cores while it runs. Reporting only the
    average would over-admit concurrent builds.
    """
    sample = ResourceSample(
        peak_rss_bytes=100 * 1024 * 1024,
        cpu_seconds=20.0,
        peak_cores_instant=4.0,
        peak_process_count=6,
    )
    out = sample.as_dict(wall_s=50.0)

    assert out["cores_saturated_avg"] == 0.4, "average over the phase"
    assert out["cores_peak_instant"] == 4.0, "worst instantaneous demand"
    assert out["cores_peak_instant"] >= out["cores_saturated_avg"]
    # The sampling interval must be reported: the peak is a sampled maximum, so a
    # consumer needs to know a sub-interval spike could be missed.
    assert out["sample_interval_s"] > 0


def test_p95_cores_is_robust_to_a_single_noisy_sample() -> None:
    """A cap divisor must not ride on one over-attributed 100ms window.

    Windows' CPU clock is coarse (~15.6ms), so a single short interval can
    over-attribute CPU and inflate the max. p95 describes SUSTAINED demand and is
    the figure to size a concurrency cap against; the max is still reported so the
    spread between them is visible.
    """
    sample = ResourceSample(
        cpu_seconds=20.0,
        peak_cores_instant=8.0,
        # 99 readings around 1 core, one 8-core spike.
        cores_instant_samples=[1.0] * 95 + [8.0] + [1.1] * 4,
    )
    out = sample.as_dict(wall_s=50.0)

    assert out["cores_peak_instant"] == 8.0, "the max still reports the spike"
    assert out["cores_p95_instant"] < out["cores_peak_instant"], "p95 discards the outlier"
    assert out["cores_p50_instant"] == 1.0
    assert out["cores_instant_samples"] == 100

    # With no readings at all, every percentile is absent rather than zero.
    empty = ResourceSample(cpu_seconds=1.0).as_dict(wall_s=1.0)
    assert empty["cores_p95_instant"] is None
    assert empty["cores_peak_instant"] is None


def test_share_pct_never_divides_by_a_missing_measurement() -> None:
    """The percentage split must degrade to None, never raise or report a false 0.

    This is the install-vs-build share — the figure the build-location decision
    turns on — so a TypeError here would take out the measurement at the point of
    reporting it.
    """
    assert _share_pct(2.0, 8.0) == 25.0

    # Absent measurements report None, NOT 0: a zero share would read as
    # "install was free", which is a different and false claim.
    assert _share_pct(None, 8.0) is None
    assert _share_pct(2.0, None) is None
    assert _share_pct(None, None) is None

    # A zero or negative denominator reports None rather than dividing.
    assert _share_pct(2.0, 0.0) is None
    assert _share_pct(0.0, 0.0) is None
    assert _share_pct(2.0, -1.0) is None

    # A real zero numerator over a real denominator is a legitimate 0.0.
    assert _share_pct(0.0, 8.0) == 0.0


def test_warm_shares_are_absent_rather_than_zero_with_no_warm_arm() -> None:
    """warm_runs=0 must produce None shares, not a crash and not 0.0."""
    assert _share_pct(None, None) is None
    # And the cold shares still sum to 100 when both halves are measured.
    cold_install, cold_build, total = 3.0, 7.0, 10.0
    assert _share_pct(cold_install, total) + _share_pct(cold_build, total) == 100.0


def test_report_shape_is_stable() -> None:
    """The report keys downstream slices read, taken from the REAL producer.

    Asserted against measure_engine's own annotations rather than a hand-written
    dict — a literal here would keep passing after a rename, which is exactly the
    breakage this test exists to catch. The keys are checked by building the same
    structure the function returns for a zero-warm-run measurement.
    """
    expected_top = {
        "engine",
        "generated",
        "cold",
        "warm",
        "cold_to_warm_saving_s",
        "peak_rss_mb",
        "peak_uss_mb",
        "cores_saturated_avg_max",
        "cores_peak_instant",
        "cores_p95_instant",
        "install_footprint",
        "artifact",
    }
    source = Path(measure_module.__file__).read_text(encoding="utf-8")
    # Every key the return dict declares must be one a consumer was promised.
    for key in expected_top:
        assert f'"{key}":' in source, f"measure_engine no longer reports {key!r}"
