"""Lightweight request-timing middleware for ee/cloud.

Records `(method, path) -> duration_ms` samples in an in-memory ring
buffer. A `report()` function dumps p50/p95/p99 on demand. Deliberately
not a metrics system - no Prometheus, no histograms, no exporters -
because the goal is just to have *some* data when the perf phase
begins. Phase 11 may swap in a real metrics stack; until then this
keeps the slice small.

Capacity defaults to 10k samples per endpoint. With the default
capacity the buffer uses ~80 KB per distinct endpoint at steady state
(two `float` per sample is conservative).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Final

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_DEFAULT_CAPACITY: Final = 10_000
_buffers: dict[tuple[str, str], deque[float]] = {}


class TimingMiddleware(BaseHTTPMiddleware):
    """Records request duration per `(method, path)`."""

    def __init__(self, app: FastAPI, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__(app)
        self.capacity = capacity

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0
        # Prefer the matched route template (e.g. /workspaces/{id}) so we
        # don't get one buffer per id; fall back to the raw URL path.
        scope_route = request.scope.get("route")
        path = (
            scope_route.path
            if scope_route is not None and hasattr(scope_route, "path")
            else request.url.path
        )
        key = (request.method, path)
        buf = _buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self.capacity)
            _buffers[key] = buf
        buf.append(duration_ms)
        return response


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
    "TimingMiddleware",
    "percentiles",
    "report",
    "reset_buffers",
    "snapshot",
]
