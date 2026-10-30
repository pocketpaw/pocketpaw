"""In-memory request-timing buffers behind ``GET /api/v1/_admin/perf``.

``record()`` keeps ``(method, path) -> duration_ms`` samples in a ring buffer
per key; ``snapshot()`` / ``percentiles()`` / ``report()`` read them back.
Deliberately not a metrics system: no Prometheus, no histograms, no exporters.
There is no middleware here: ``_core.request_log.RequestLogMiddleware`` already
times every HTTP request and calls ``record()`` with the time to the response
headers, for skipped (health/static) paths too.

Capacity is 10k samples per key (~80 KB each at steady state). That bound only
holds if the key space is bounded: a MATCHED request keys on its route
template, fixed at startup; every UNMATCHED request shares ``UNMATCHED_PATH``,
because a 404's raw path is attacker-chosen and would otherwise mint one
permanent buffer per distinct URL.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Final

_DEFAULT_CAPACITY: Final = 10_000

#: Key for every request that matched no route. Collapsing them is the whole
#: bound: a 404's path is attacker-chosen, a route template is not.
UNMATCHED_PATH: Final = "<unmatched>"

_buffers: dict[tuple[str, str], deque[float]] = {}


def record(method: str, route: Any, duration_ms: float, capacity: int = _DEFAULT_CAPACITY) -> None:
    """Append one sample, keyed on the matched route template (``scope["route"]``)."""
    path = route.path if route is not None and hasattr(route, "path") else UNMATCHED_PATH
    key = (method, path)
    buf = _buffers.get(key)
    if buf is None:
        buf = deque(maxlen=capacity)
        _buffers[key] = buf
    buf.append(duration_ms)


def reset_buffers() -> None:
    """Clear all collected timings (for tests)."""
    _buffers.clear()


def snapshot() -> dict[tuple[str, str], list[float]]:
    """Return a copy of current buffers as plain lists.

    Copies are O(n) per endpoint; cheap for the default capacity but
    don't call this in a hot path.
    """
    return {key: list(buf) for key, buf in _buffers.items()}


def percentiles(
    samples: list[float],
    qs: tuple[float, ...] = (0.5, 0.95, 0.99),
) -> dict[float, float]:
    """Compute the requested percentiles from `samples`. Linear sort is
    fine for the default capacity (~10k items)."""
    if not samples:
        return {q: 0.0 for q in qs}
    sorted_samples = sorted(samples)
    n = len(sorted_samples)
    out: dict[float, float] = {}
    for q in qs:
        idx = min(n - 1, max(0, round(q * (n - 1))))
        out[q] = sorted_samples[idx]
    return out


def report() -> str:
    """Format current snapshot as a human-readable table."""
    snap = snapshot()
    header = f"{'METHOD':<7} {'PATH':<60} {'COUNT':>6} {'p50':>10} {'p95':>10} {'p99':>10}"
    lines = [header]
    for (method, path), samples in sorted(snap.items()):
        pcts = percentiles(samples)
        lines.append(
            f"{method:<7} {path:<60} {len(samples):>6} "
            f"{pcts[0.5]:>8.2f}ms {pcts[0.95]:>8.2f}ms {pcts[0.99]:>8.2f}ms"
        )
    return "\n".join(lines)


__all__ = [
    "UNMATCHED_PATH",
    "percentiles",
    "record",
    "report",
    "reset_buffers",
    "snapshot",
]
