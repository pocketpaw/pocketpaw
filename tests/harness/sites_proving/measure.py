"""Resident sidecar vs short-lived-process-per-render — the honest measurement.

Created for SG-1 (sites proving harness).

WHAT: renders the SAME spec N times through each driver and reports cold and warm
timings for both.

Definitions, stated because the two arms mean different things by "cold":

* **sidecar cold** — process spawn + ``import(entry.js)``, measured at startup
  before any render. Paid ONCE per sidecar lifetime.
* **sidecar warm** — one render on the already-imported bundle. What every
  publish after the first costs.
* **per-render cold** — the first render, which pays spawn + import + render.
* **per-render warm** — later renders. Named "warm" only because the OS file
  cache is warm; each still pays spawn + import, so this is the number that
  decides the architecture.

WHY this decides something real: if per-render warm is close to sidecar warm, the
simpler stateless model wins (no resident process to supervise, crash-isolated per
render). If it is far off, the sidecar's complexity is bought with real latency.
The numbers go in the report either way — the point is to answer the question, not
to justify a preferred answer.

Reported statistics are min / median / p95 / mean over the sample. Min matters
because it is the floor the architecture allows; median is the typical publish.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from typing import Any

from .renderer import PerRenderRenderer, SidecarRenderer, SiteTokens
from .verify import verify

# Enough samples for a stable median without making the suite slow: the
# per-render arm pays a full process spawn per sample.
DEFAULT_SAMPLES = 5


def _stats(samples: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "min_ms": round(ordered[0], 2),
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 2),
        "mean_ms": round(statistics.fmean(ordered), 2),
        "max_ms": round(ordered[-1], 2),
    }


def _time_render(renderer: Any, spec: Any, tokens: SiteTokens) -> tuple[float, Any]:
    started = time.perf_counter()
    bundle = renderer.render(spec, tokens)
    return (time.perf_counter() - started) * 1000.0, bundle


def measure_sidecar_vs_per_render(
    spec: Any, tokens: SiteTokens, *, samples: int = DEFAULT_SAMPLES
) -> dict[str, Any]:
    """Run both arms and return a JSON-safe measurement block.

    Every render in both arms is VERIFIED, so a fast arm cannot win by producing
    nothing — a timing on unverified output would be worthless.
    """
    if samples < 2:
        raise ValueError("need at least 2 samples to separate cold from warm")

    # --- Arm 1: resident sidecar -------------------------------------------
    sidecar = SidecarRenderer()
    sidecar.start()  # cold start recorded inside start()
    sidecar_cold_start_ms = sidecar.cold_start_ms or 0.0
    try:
        first_ms, bundle = _time_render(sidecar, spec, tokens)
        verify(bundle, expected_form_action=tokens.form_action)

        warm: list[float] = []
        for _ in range(samples):
            elapsed, bundle = _time_render(sidecar, spec, tokens)
            verify(bundle, expected_form_action=tokens.form_action)
            warm.append(elapsed)
    finally:
        sidecar.stop()

    sidecar_block = {
        "process_start_plus_import_ms": round(sidecar_cold_start_ms, 2),
        "first_render_ms": round(first_ms, 2),
        # What a publish costs once the sidecar is up: the render alone.
        "cold_total_ms": round(sidecar_cold_start_ms + first_ms, 2),
        "warm": _stats(warm),
    }

    # --- Arm 2: a fresh process per render ---------------------------------
    per_render = PerRenderRenderer()
    cold_ms, bundle = _time_render(per_render, spec, tokens)
    verify(bundle, expected_form_action=tokens.form_action)

    per_render_warm: list[float] = []
    for _ in range(samples):
        elapsed, bundle = _time_render(per_render, spec, tokens)
        verify(bundle, expected_form_action=tokens.form_action)
        per_render_warm.append(elapsed)

    per_render_block = {
        # Each sample includes spawn + import + render; they are not separable
        # from outside the process, which is exactly the cost being measured.
        "cold_total_ms": round(cold_ms, 2),
        "warm": _stats(per_render_warm),
    }

    sidecar_warm = sidecar_block["warm"]["median_ms"]
    per_render_median = per_render_block["warm"]["median_ms"]
    overhead_ms = round(per_render_median - sidecar_warm, 2)

    return {
        "samples": samples,
        "sidecar": sidecar_block,
        "per_render": per_render_block,
        "per_render_overhead_vs_sidecar_ms": overhead_ms,
        "per_render_slowdown_x": round(per_render_median / sidecar_warm, 1)
        if sidecar_warm > 0
        else None,
        # The comparison a later slice actually needs: against the 45-60s the
        # current per-site build spends inside the HTTP request.
        "legacy_per_site_build_s": "45-60",
        "verified_every_render": True,
    }
