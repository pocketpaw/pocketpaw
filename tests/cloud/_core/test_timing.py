"""Tests for ee.cloud._core.timing — request-timing middleware + percentiles."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.timing import (
    TimingMiddleware,
    percentiles,
    report,
    reset_buffers,
    snapshot,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_buffers()
    yield
    reset_buffers()


def test_percentiles_empty_returns_zero_for_each() -> None:
    assert percentiles([]) == {0.5: 0.0, 0.95: 0.0, 0.99: 0.0}


def test_percentiles_single_sample() -> None:
    pcts = percentiles([42.0])
    assert pcts == {0.5: 42.0, 0.95: 42.0, 0.99: 42.0}


def test_percentiles_sorted_correctly() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    pcts = percentiles(samples, qs=(0.5, 0.9, 1.0))
    assert pcts[0.5] == pytest.approx(5.0, abs=0.5)
    assert pcts[0.9] == pytest.approx(9.0, abs=0.5)
    assert pcts[1.0] == 10.0


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TimingMiddleware)

    @app.get("/fast")
    def _fast() -> dict:
        return {"ok": True}

    @app.get("/slow")
    def _slow() -> dict:
        time.sleep(0.005)
        return {"ok": True}

    return app


def test_middleware_records_durations_per_endpoint() -> None:
    client = TestClient(_build_app())
    for _ in range(3):
        client.get("/fast")
    client.get("/slow")

    snap = snapshot()
    fast_key = ("GET", "/fast")
    slow_key = ("GET", "/slow")

    assert fast_key in snap
    assert slow_key in snap
    assert len(snap[fast_key]) == 3
    assert len(snap[slow_key]) == 1

    # Slow endpoint should be at least ~5ms; allow some scheduler slack
    assert snap[slow_key][0] >= 4.0


def test_reset_buffers_clears_state() -> None:
    client = TestClient(_build_app())
    client.get("/fast")
    assert snapshot()
    reset_buffers()
    assert snapshot() == {}


def test_ring_buffer_caps_at_capacity() -> None:
    app = FastAPI()
    # Tiny capacity for the test
    app.add_middleware(TimingMiddleware, capacity=5)

    @app.get("/x")
    def _x() -> dict:
        return {"ok": True}

    client = TestClient(app)
    for _ in range(20):
        client.get("/x")

    snap = snapshot()
    assert len(snap[("GET", "/x")]) == 5


def test_report_includes_collected_endpoints() -> None:
    client = TestClient(_build_app())
    client.get("/fast")
    out = report()
    assert "GET" in out
    assert "/fast" in out
    assert "p50" in out and "p95" in out and "p99" in out


def test_report_empty_returns_header_only() -> None:
    out = report()
    # Header line still printed; no data rows
    assert "p50" in out
    # Only one line (the header) when there's no data
    assert len(out.splitlines()) == 1
