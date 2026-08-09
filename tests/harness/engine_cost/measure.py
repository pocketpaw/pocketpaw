"""Per-engine publish cost: what a real build of each site engine actually costs.

Created for SR-1.

WHAT: for each engine (react, svelte, ripple-static, ripple-dynamic) this
generates a real project from paw-sites' own fixtures and runs the real
``bun install`` + ``bun run build``, recording:

* cold total (empty node_modules) and warm total (populated node_modules),
* the install share vs the build share of each,
* peak RSS across the whole process tree,
* CPU cores saturated (CPU-seconds consumed / wall seconds).

WHY react is the point: the 45-60s static and 4m47s dynamic figures on record are
ripple/svelte only. React is a Vite SSG whose ``bun run build`` is THREE vite
passes plus a prerender step, and it has never been measured. Concurrency caps,
Daytona cost/benefit and burst headroom all rest on numbers that currently omit
the newest engine.

WHY the phases are timed separately rather than as one publish number: install
and build have different remedies. Install cost is fixed by a warm cache or a
baked image; build cost is not, and only the build share responds to a faster
machine or a shared renderer. A single blended number hides which lever applies.

HOW cold vs warm are made honest: cold deletes node_modules AND the lockfile, so
bun must resolve from scratch. Warm runs the same project again with node_modules
intact — which is what PERF-3's per-pocket build-dir reuse produces in production
(``install_inputs_hash`` lets a republish skip install when package.json and the
lockfile are unchanged). bun's global package cache is NOT cleared: production
never runs with a cold global cache either, and clearing it would measure network
latency rather than publish cost. That caveat is recorded in the report rather
than hidden.

Sampling: RSS and CPU are polled from a background thread over the whole process
tree (bun spawns vite, which spawns workers), so a short-lived child's peak is
not missed between phases.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

HERE = Path(__file__).resolve().parent
GENERATE_JS = HERE / "node" / "generate.mjs"

# The four engines under measurement. ripple splits into two because the dynamic
# path scaffolds a D1 migration + remote functions and is on record as far
# slower (4m47s) than the static path (45-60s) — one "ripple" number would be
# meaningless.
ENGINES: tuple[str, ...] = ("react", "svelte", "ripple-static", "ripple-dynamic")

# paw-sites is the generator's own repo; generate.mjs runs with this as cwd so
# bun resolves the generator's node_modules. READ-ONLY — nothing is written here.
SITES_REPO = Path(os.environ.get("PAW_SITES_REPO", "D:/paw-workspace/paw-sites"))

# Ceiling per phase. The dynamic ripple build is on record at 4m47s, so a phase
# timeout has to sit well above that or the measurement truncates the very case
# it exists to quantify.
PHASE_TIMEOUT_S = float(os.environ.get("SR1_PHASE_TIMEOUT_S", "900"))

# Sampling interval for the RSS/CPU poller. 100ms is fine enough to catch a
# vite pass's peak without the poller itself distorting the measurement.
SAMPLE_INTERVAL_S = 0.1


class PhaseFailed(RuntimeError):
    """A generate/install/build phase exited non-zero. Carries the captured output."""


@dataclass
class ResourceSample:
    """Peak RSS and CPU consumed by a process tree over one phase."""

    peak_rss_bytes: int = 0
    # Summed USS (unique set size) — memory private to each process. Summing RSS
    # across a tree DOUBLE-COUNTS pages shared between parent and children (bun's
    # own binary, shared libs), so RSS overstates the real footprint. USS is the
    # figure to use when sizing how many concurrent builds fit in RAM; RSS is kept
    # as the conservative upper bound. Both are reported rather than picking one.
    peak_uss_bytes: int = 0
    cpu_seconds: float = 0.0
    samples: int = 0
    # Highest simultaneous process count seen — how much parallelism the phase
    # actually spawned, which matters for sizing a concurrency cap.
    peak_process_count: int = 0

    def as_dict(self, wall_s: float) -> dict[str, Any]:
        return {
            "peak_rss_mb": round(self.peak_rss_bytes / (1024 * 1024), 1),
            "peak_uss_mb": round(self.peak_uss_bytes / (1024 * 1024), 1)
            if self.peak_uss_bytes
            else None,
            "cpu_seconds": round(self.cpu_seconds, 2),
            # CPU-seconds / wall-seconds = average cores saturated. 1.0 means one
            # core pegged for the whole phase; 4.0 means four cores' worth.
            "cores_saturated_avg": round(self.cpu_seconds / wall_s, 2) if wall_s > 0 else None,
            "peak_process_count": self.peak_process_count,
            "samples": self.samples,
        }


class _TreeMonitor:
    """Polls a process tree for peak RSS and total CPU from a background thread.

    Walks children on every tick because bun spawns vite which spawns its own
    workers — sampling only the root would miss nearly all of the real cost.
    CPU is read as the tree's cumulative user+system time, so a child that exits
    between ticks still has its CPU counted via the parent's accumulated total
    where the OS provides it; peak RSS is inherently a sampled maximum, which is
    stated in the report rather than claimed as exact.
    """

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.result = ResourceSample()

    def _tick(self) -> None:
        try:
            root = psutil.Process(self._pid)
        except psutil.NoSuchProcess:
            return

        try:
            procs = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            return

        rss = 0
        uss = 0
        cpu = 0.0
        alive = 0
        for proc in procs:
            try:
                with proc.oneshot():
                    rss += proc.memory_info().rss
                    times = proc.cpu_times()
                    cpu += times.user + times.system
                    try:
                        # USS needs a second syscall and can fail on a process
                        # exiting mid-read; a miss degrades the USS total for this
                        # tick rather than losing the whole sample.
                        uss += proc.memory_full_info().uss
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        pass
                alive += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        self.result.peak_rss_bytes = max(self.result.peak_rss_bytes, rss)
        self.result.peak_uss_bytes = max(self.result.peak_uss_bytes, uss)
        # CPU is cumulative per process, so the tree total is monotonic while
        # processes live; take the max so a tick that raced an exit cannot
        # under-report.
        self.result.cpu_seconds = max(self.result.cpu_seconds, cpu)
        self.result.peak_process_count = max(self.result.peak_process_count, alive)
        self.result.samples += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(SAMPLE_INTERVAL_S)
        self._tick()  # final read, to catch a peak just before exit

    def __enter__(self) -> _TreeMonitor:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


@dataclass
class PhaseResult:
    """One timed phase."""

    name: str
    wall_s: float
    exit_code: int
    resources: dict[str, Any] = field(default_factory=dict)
    stdout_tail: str = ""
    stderr_tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.name,
            "wall_s": round(self.wall_s, 2),
            "exit_code": self.exit_code,
            **self.resources,
        }


def _run_phase(name: str, argv: list[str], cwd: Path) -> PhaseResult:
    """Run one phase under the resource monitor, timing wall clock."""
    started = time.perf_counter()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with _TreeMonitor(proc.pid) as monitor:
        try:
            stdout, stderr = proc.communicate(timeout=PHASE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise PhaseFailed(f"{name} exceeded {PHASE_TIMEOUT_S}s\n{stderr[-2000:]}") from None
    wall_s = time.perf_counter() - started

    result = PhaseResult(
        name=name,
        wall_s=wall_s,
        exit_code=proc.returncode,
        resources=monitor.result.as_dict(wall_s),
        stdout_tail=stdout[-2000:],
        stderr_tail=stderr[-2000:],
    )
    if proc.returncode != 0:
        raise PhaseFailed(
            f"{name} exited {proc.returncode}\n"
            f"--- stdout tail ---\n{result.stdout_tail}\n"
            f"--- stderr tail ---\n{result.stderr_tail}"
        )
    return result


def _bun() -> str:
    exe = shutil.which("bun")
    if not exe:
        raise PhaseFailed("bun not found on PATH")
    return exe


def generate_project(engine: str, out_dir: Path) -> dict[str, Any]:
    """Materialize one engine's project. Returns generate.mjs's JSON line."""
    result = _run_phase(
        f"{engine}:generate",
        [_bun(), str(GENERATE_JS), engine, str(out_dir).replace("\\", "/")],
        cwd=SITES_REPO,
    )
    line = result.stdout_tail.strip().splitlines()[-1]
    payload = json.loads(line)
    payload["_phase"] = result.as_dict()
    return payload


def _clear_install_state(project_dir: Path) -> None:
    """Make the next install genuinely cold: no node_modules, no lockfile.

    bun's GLOBAL cache is deliberately left alone — production never publishes
    with a cold global cache, and clearing it would fold network download time
    into a number meant to describe publish cost on a warm box.
    """
    shutil.rmtree(project_dir / "node_modules", ignore_errors=True)
    for lock in ("bun.lock", "bun.lockb", "package-lock.json", "yarn.lock"):
        (project_dir / lock).unlink(missing_ok=True)


def _clear_build_output(project_dir: Path, engine: str) -> None:
    """Remove prior build output so a warm build is a real build, not a no-op.

    Both output shapes are cleared regardless of engine: react emits dist/ +
    .paw-ssr/, ripple/svelte emit .svelte-kit/. Clearing .svelte-kit also avoids
    the Windows EBUSY class of failure that generator_client's
    _clear_stale_svelte_kit exists to prevent on a republish.
    """
    for rel in ("dist", ".paw-ssr", ".svelte-kit"):
        shutil.rmtree(project_dir / rel, ignore_errors=True)


def measure_engine(engine: str, work_root: Path, *, warm_runs: int = 1) -> dict[str, Any]:
    """Generate, then time a cold install+build and ``warm_runs`` warm ones."""
    project_dir = work_root / engine
    shutil.rmtree(project_dir, ignore_errors=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    generated = generate_project(engine, project_dir)
    bun = _bun()

    # --- cold: nothing installed, nothing built --------------------------
    _clear_install_state(project_dir)
    _clear_build_output(project_dir, engine)
    cold_install = _run_phase(f"{engine}:cold-install", [bun, "install"], cwd=project_dir)
    cold_build = _run_phase(f"{engine}:cold-build", [bun, "run", "build"], cwd=project_dir)

    # --- warm: node_modules kept, build output cleared -------------------
    warm_samples: list[dict[str, Any]] = []
    for index in range(warm_runs):
        _clear_build_output(project_dir, engine)
        # Install still RUNS on the warm arm (rather than being skipped) so its
        # warm cost is measured rather than assumed to be zero — production's
        # PERF-3 skip is a separate decision, and this shows what it saves.
        warm_install = _run_phase(
            f"{engine}:warm-install-{index}", [bun, "install"], cwd=project_dir
        )
        warm_build = _run_phase(
            f"{engine}:warm-build-{index}", [bun, "run", "build"], cwd=project_dir
        )
        warm_samples.append(
            {
                "install": warm_install.as_dict(),
                "build": warm_build.as_dict(),
                "total_s": round(warm_install.wall_s + warm_build.wall_s, 2),
            }
        )

    # Disk is a real concurrency limit too: N simultaneous builds each need their
    # own node_modules unless the tree is shared, and these differ ~10x by engine.
    install_footprint = _dir_footprint(project_dir / "node_modules")

    cold_total = cold_install.wall_s + cold_build.wall_s
    warm_totals = [s["total_s"] for s in warm_samples]
    warm_median = statistics.median(warm_totals) if warm_totals else None
    warm_build_median = (
        statistics.median([s["build"]["wall_s"] for s in warm_samples]) if warm_samples else None
    )
    warm_install_median = (
        statistics.median([s["install"]["wall_s"] for s in warm_samples]) if warm_samples else None
    )

    artifact = _describe_artifact(project_dir, generated)

    return {
        "engine": engine,
        "generated": {k: v for k, v in generated.items() if k not in {"_phase"}},
        "cold": {
            "install": cold_install.as_dict(),
            "build": cold_build.as_dict(),
            "total_s": round(cold_total, 2),
            "install_share_pct": round(100 * cold_install.wall_s / cold_total, 1)
            if cold_total
            else None,
            "build_share_pct": round(100 * cold_build.wall_s / cold_total, 1)
            if cold_total
            else None,
        },
        "warm": {
            "runs": warm_samples,
            "median_total_s": warm_median,
            "median_install_s": round(warm_install_median, 2)
            if warm_install_median is not None
            else None,
            "median_build_s": round(warm_build_median, 2)
            if warm_build_median is not None
            else None,
            "install_share_pct": round(100 * warm_install_median / warm_median, 1)
            if warm_median
            else None,
            "build_share_pct": round(100 * warm_build_median / warm_median, 1)
            if warm_median
            else None,
        },
        "cold_to_warm_saving_s": round(cold_total - warm_median, 2)
        if warm_median is not None
        else None,
        "peak_rss_mb": max(
            cold_install.resources["peak_rss_mb"],
            cold_build.resources["peak_rss_mb"],
            *[s["build"]["peak_rss_mb"] for s in warm_samples] or [0],
        ),
        # The number to size concurrency against — summed RSS double-counts pages
        # shared across the process tree, USS does not.
        "peak_uss_mb": max(
            filter(
                None,
                [
                    cold_install.resources.get("peak_uss_mb"),
                    cold_build.resources.get("peak_uss_mb"),
                    *[s["build"].get("peak_uss_mb") for s in warm_samples],
                ],
            ),
            default=None,
        ),
        "cores_saturated_peak": max(
            filter(
                None,
                [
                    cold_install.resources["cores_saturated_avg"],
                    cold_build.resources["cores_saturated_avg"],
                    *[s["build"]["cores_saturated_avg"] for s in warm_samples],
                ],
            ),
            default=None,
        ),
        "install_footprint": install_footprint,
        "artifact": artifact,
    }


def _dir_footprint(path: Path) -> dict[str, Any]:
    """Size of an installed dependency tree — disk cost per concurrent build."""
    if not path.exists():
        return {"exists": False}
    total = 0
    files = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
                files += 1
        except OSError:
            # A long path or a file vanishing mid-walk should not abort the walk;
            # a slight undercount is better than losing the whole measurement.
            continue
    return {
        "exists": True,
        "size_mb": round(total / (1024 * 1024), 1),
        "file_count": files,
        "top_level_packages": sum(1 for _ in path.iterdir()),
    }


def _describe_artifact(project_dir: Path, generated: dict[str, Any]) -> dict[str, Any]:
    """What the build actually emitted — proof the timing covers a real build.

    A build that exits 0 while producing nothing would otherwise look like the
    fastest engine. Recording the output tree's size and entry HTML makes a
    hollow build visible in the report.

    ``entry_html_bytes`` is legitimately ``None`` for a DYNAMIC ripple site: its
    page is SSR'd (prerender off), so the deployable tree carries a ``_worker.js``
    that renders per request and no prerendered ``index.html``. Verified on the
    measured artifact. So "did this build produce something real" is
    ``served_something``: a prerendered entry OR a server worker — never
    entry HTML alone, which would fail a healthy dynamic build.
    """
    candidates = {
        "dist": project_dir / "dist",
        ".svelte-kit/cloudflare": project_dir / ".svelte-kit" / "cloudflare",
    }
    for label, path in candidates.items():
        if not path.exists():
            continue
        files = [p for p in path.rglob("*") if p.is_file()]
        entry = path / "index.html"
        has_worker = (path / "_worker.js").exists() or (path / "_worker.js" / "index.js").exists()
        entry_bytes = entry.stat().st_size if entry.exists() else None
        return {
            "output_dir": label,
            "file_count": len(files),
            "total_bytes": sum(p.stat().st_size for p in files),
            "entry_html_bytes": entry_bytes,
            "has_worker_js": has_worker,
            "served_something": bool(entry_bytes) or has_worker,
            "render_mode": "prerendered" if entry_bytes else ("ssr" if has_worker else "none"),
        }
    return {"output_dir": None, "note": "no recognized output dir", "generated": generated}


def measure_all(
    work_root: Path, *, engines: tuple[str, ...] = ENGINES, warm_runs: int = 1
) -> dict[str, Any]:
    """Measure every engine. A failing engine is RECORDED, not fatal.

    One engine failing to build is itself a finding worth reporting — and it must
    not cost the numbers for the other three.
    """
    results: dict[str, Any] = {}
    for engine in engines:
        try:
            results[engine] = measure_engine(engine, work_root, warm_runs=warm_runs)
        except PhaseFailed as exc:
            results[engine] = {"engine": engine, "failed": True, "error": str(exc)[:4000]}

    return {
        "engines": results,
        "host": host_info(),
        "method": {
            "cold": "node_modules + lockfile deleted; bun global cache NOT cleared",
            "warm": "node_modules retained, build output cleared, install re-run",
            "install_and_build": (
                "bun install, then bun run build (the control plane's own commands)"
            ),
            "generate_excluded_from_totals": True,
            "fixtures": "paw-sites/tests/fixtures — the repo's own real-build fixtures",
            "ripple_dep_rewrite": "mirrors generator_client.py::_rewrite_ripple_dep (+motion)",
            "rss_note": (
                "peak RSS is a 100ms-sampled maximum over the process tree, not an exact peak"
            ),
        },
    }


def host_info() -> dict[str, Any]:
    """The box these numbers describe. Without it they don't transfer."""
    memory = psutil.virtual_memory()
    return {
        "logical_cores": psutil.cpu_count(logical=True),
        "physical_cores": psutil.cpu_count(logical=False),
        "total_ram_gb": round(memory.total / (1024**3), 1),
        "available_ram_gb": round(memory.available / (1024**3), 1),
        "bun_version": _tool_version(["bun", "--version"]),
        "node_version": _tool_version(["node", "--version"]),
        "platform": f"{os.name} {os.environ.get('OS', '')}".strip(),
    }


def _tool_version(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, *argv[1:]], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None
