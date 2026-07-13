# Tests for the leading-edge push coalescer (feat/push-user-message-notifications).
# Created: 2026-07-02 — proves the throttle contract without real sockets or DB:
#   - leading edge fires immediately (zero added latency);
#   - a burst on one key collapses to leading + one trailing flush;
#   - a sustained stream is capped at one send per window;
#   - distinct keys throttle independently;
#   - window <= 0 disables coalescing (pass-through).
# Uses a tiny window (0.05s) and real asyncio.sleep so the timing is exercised
# for real; ``reset()`` between cases prevents window state from leaking.
#
# Updated 2026-07-02: the leading edge now fires DETACHED (a background task) so
# ``submit`` returns without blocking the caller. It still fires "immediately"
# (next loop tick, sub-ms), but the tests must ``_drain()`` one tick before
# asserting the leading emit has landed.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.push import coalesce

# Small enough to keep the suite fast; large enough to be robust on a loaded CI.
_WINDOW = 0.05


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", str(_WINDOW))
    await coalesce.aclose()
    yield
    # Await the cancellations so a still-sleeping window task can't schedule a
    # callback on the loop after it closes.
    await coalesce.aclose()


def _counter():
    calls: list[int] = []

    async def emit() -> None:
        calls.append(1)

    return calls, emit


async def _drain() -> None:
    """Yield a loop tick so detached leading-edge emits run before we assert.

    The leading edge is fired via ``asyncio.create_task``, so it runs on the
    next tick rather than synchronously inside ``submit``. One short sleep lets
    every currently-ready leading emit complete without waiting for a window.
    """
    await asyncio.sleep(0.01)


async def test_leading_edge_fires_immediately() -> None:
    calls, emit = _counter()
    await coalesce.submit(("w", "u", "g"), emit)
    await _drain()
    # Fired on the leading edge (detached, next tick) — no wait for the window.
    assert len(calls) == 1


async def test_burst_collapses_to_leading_plus_one() -> None:
    calls, emit = _counter()
    key = ("w", "u", "g")
    for _ in range(10):
        await coalesce.submit(key, emit)

    await _drain()
    # Only the leading push has gone out so far; the other 9 are suppressed.
    assert len(calls) == 1

    # Let the window elapse: exactly one trailing flush for the whole burst.
    await asyncio.sleep(_WINDOW * 3)
    assert len(calls) == 2


async def test_sustained_stream_capped_per_window() -> None:
    calls, emit = _counter()
    key = ("w", "u", "g")

    # Leading push.
    await coalesce.submit(key, emit)
    await _drain()
    assert len(calls) == 1

    # Two more windows, each fed a suppressed message → each yields one flush.
    for _ in range(2):
        await coalesce.submit(key, emit)  # suppressed, remembered
        await asyncio.sleep(_WINDOW * 2)  # window elapses → one trailing flush

    # 1 leading + 2 per-window trailing flushes.
    assert len(calls) == 3


async def test_distinct_keys_are_independent() -> None:
    calls, emit = _counter()
    await coalesce.submit(("w", "alice", "g"), emit)
    await coalesce.submit(("w", "bob", "g"), emit)
    await _drain()
    # Two different recipients → two leading pushes, neither suppresses the other.
    assert len(calls) == 2


async def test_disabled_is_passthrough(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "0")
    calls, emit = _counter()
    key = ("w", "u", "g")
    for _ in range(3):
        await coalesce.submit(key, emit)
    # No throttling: every submit emits immediately.
    assert len(calls) == 3


async def test_malformed_window_uses_default(monkeypatch) -> None:
    # A bad env value must not break delivery: it falls back to the default
    # (a positive window), so the first submit still fires on the leading edge.
    monkeypatch.setenv("CLOUD_PUSH_COALESCE_SECONDS", "not-a-number")
    calls, emit = _counter()
    await coalesce.submit(("w", "u", "g"), emit)
    await _drain()
    assert len(calls) == 1
