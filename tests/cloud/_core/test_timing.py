"""Tests for ee.cloud._core.timing — the /_admin/perf buffers + percentiles.

The samples are recorded by ``RequestLogMiddleware`` (there is no separate
timing middleware), so the endpoint tests drive it. Its request_logs write is
patched out; only the timing side is under test here.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.request_log import RequestLogMiddleware
from pocketpaw_ee.cloud._core.timing import (
    UNMATCHED_PATH,
    percentiles,
    record,
    report,
    reset_buffers,
    snapshot,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_buffers()
    with patch("pocketpaw_ee.cloud._core.request_log._log_request"):
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
    app.add_middleware(RequestLogMiddleware)

    @app.get("/fast")
    def _fast() -> dict:
        return {"ok": True}

    @app.get("/slow")
    def _slow() -> dict:
        time.sleep(0.005)
        return {"ok": True}

    @app.get("/health")
    def _health() -> dict:
        return {"ok": True}

    @app.get("/stream")
    async def _stream() -> StreamingResponse:
        async def _body():
            yield b"head"
            time.sleep(0.05)  # after the headers went out
            yield b"tail"

        return StreamingResponse(_body())

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


def test_paths_skipped_from_the_request_log_are_still_timed() -> None:
    client = TestClient(_build_app())
    client.get("/health")
    assert len(snapshot()[("GET", "/health")]) == 1


def test_unmatched_requests_share_one_bucket() -> None:
    client = TestClient(_build_app())
    client.get("/nope/1")
    client.get("/nope/2")
    assert list(snapshot()) == [("GET", UNMATCHED_PATH)]
    assert len(snapshot()[("GET", UNMATCHED_PATH)]) == 2


def test_a_streamed_response_is_timed_to_its_headers() -> None:
    client = TestClient(_build_app())
    assert client.get("/stream").content == b"headtail"
    # The body sleeps 50ms after the headers; time-to-headers excludes it.
    assert snapshot()[("GET", "/stream")][0] < 40.0


def test_reset_buffers_clears_state() -> None:
    client = TestClient(_build_app())
    client.get("/fast")
    assert snapshot()
    reset_buffers()
    assert snapshot() == {}


def test_ring_buffer_caps_at_capacity() -> None:
    route = type("R", (), {"path": "/x"})()
    for i in range(20):
        record("GET", route, float(i), capacity=5)
    assert snapshot()[("GET", "/x")] == [15.0, 16.0, 17.0, 18.0, 19.0]


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
