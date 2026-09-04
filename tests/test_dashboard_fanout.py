# Regression: the OSS dashboard's WebSocket broadcasts must be bounded.
#
# Created 2026-09-04 (backend-perf H1, OSS half). The cloud package learned this
# in August — see SEND_TIMEOUT_SECONDS in ee/.../chat/ws.py, added because "one
# stuck socket would stall delivery to every other member and the sender's own
# request". The OSS dashboard path never got the fix: six copies of "iterate
# active_connections, await ws.send_json, no timeout", so a client that stopped
# reading held the loop on TCP back-pressure with no deadline at all. The
# dashboard is one uvicorn process, so that await is the whole box.
#
# What each test would catch (mutations in tests/mutations/event_loop.json):
#   - drop the asyncio.wait_for wrapper  -> test_a_wedged_socket_cannot_stall
#   - go back to a serial for-loop       -> test_sends_run_concurrently
#   - stop removing timed-out sockets    -> test_a_timed_out_socket_is_dropped
#
# The last one is the subtle one. Adding a timeout WITHOUT dropping the socket
# would have made things worse, not better: a wedged client would then be
# re-attempted, and re-time-out, on every subsequent broadcast forever.

from __future__ import annotations

import asyncio

import pytest

from pocketpaw import dashboard_lifecycle as dl
from pocketpaw.dashboard_state import active_connections


class _FakeWS:
    def __init__(self, name: str, gate: asyncio.Event | None = None, hang: bool = False):
        self.name = name
        self.gate = gate
        self.hang = hang
        self.received: list[dict] = []
        self.in_flight = False

    async def send_json(self, data: dict) -> None:
        self.in_flight = True
        try:
            if self.hang:
                await asyncio.sleep(3600)
            if self.gate is not None:
                await self.gate.wait()
            self.received.append(data)
        finally:
            self.in_flight = False


@pytest.fixture(autouse=True)
def _clean_registry():
    """active_connections is a module-level singleton shared with the running
    dashboard, so leaving entries in it leaks into unrelated tests."""
    active_connections.clear()
    yield
    active_connections.clear()


async def test_sends_run_concurrently():
    gate = asyncio.Event()
    socks = [_FakeWS(f"s{i}", gate) for i in range(6)]
    active_connections.extend(socks)

    task = asyncio.create_task(dl._fanout({"type": "ping"}))
    for _ in range(12):
        await asyncio.sleep(0)

    assert sum(1 for s in socks if s.in_flight) == 6, (
        "sockets are being written one at a time; a slow client delays the rest"
    )
    gate.set()
    await task
    assert all(s.received == [{"type": "ping"}] for s in socks)


async def test_a_wedged_socket_cannot_stall_the_broadcast(monkeypatch):
    """The point of the timeout. A client that stopped reading leaves send_json
    waiting on TCP back-pressure with no deadline of its own."""
    monkeypatch.setattr(dl, "WS_SEND_TIMEOUT_SECONDS", 0.05)

    wedged = _FakeWS("wedged", hang=True)
    healthy = _FakeWS("healthy")
    active_connections.extend([wedged, healthy])

    async with asyncio.timeout(5):
        await dl._fanout({"type": "ping"})

    assert healthy.received == [{"type": "ping"}]


async def test_a_timed_out_socket_is_dropped(monkeypatch):
    """Without removal, adding the timeout would make the stall RECUR on every
    later broadcast instead of happening once."""
    monkeypatch.setattr(dl, "WS_SEND_TIMEOUT_SECONDS", 0.05)

    wedged = _FakeWS("wedged", hang=True)
    healthy = _FakeWS("healthy")
    active_connections.extend([wedged, healthy])

    async with asyncio.timeout(5):
        await dl._fanout({"type": "ping"})

    assert wedged not in active_connections, "a timed-out socket stayed in the registry"
    assert healthy in active_connections


async def test_a_raising_socket_is_dropped():
    """Unchanged behaviour from the original loops — kept under test because
    the rewrite unified two variants (one dropped, one swallowed) into one."""

    async def _explode(_data):
        raise RuntimeError("broken pipe")

    bad = _FakeWS("bad")
    bad.send_json = _explode
    good = _FakeWS("good")
    active_connections.extend([bad, good])

    await dl._fanout({"type": "ping"})

    assert bad not in active_connections
    assert good in active_connections
    assert good.received == [{"type": "ping"}]


async def test_no_connections_is_a_no_op():
    await dl._fanout({"type": "ping"})


async def test_every_broadcast_helper_goes_through_the_bounded_path(monkeypatch):
    """Six helpers each had their own copy of the unbounded loop. This asserts
    none of them grew a private one back."""
    calls: list[dict] = []

    async def _record(message: dict) -> None:
        calls.append(message)

    monkeypatch.setattr(dl, "_fanout", _record)

    await dl._broadcast_audit_entry({"id": "a1"})
    await dl._broadcast_health_update({"ok": True})
    # No "type" key in the chunk on purpose: broadcast_intention spreads the
    # chunk OVER its own envelope, so a chunk carrying "type" overwrites
    # "intention_event". Pre-existing behaviour, not something this change
    # introduced — but it makes a careless fixture here assert the wrong thing.
    await dl.broadcast_intention("i1", {"content": "hello"})
    await dl.push_open_path("/tmp/x", action="view")

    assert [c["type"] for c in calls] == [
        "system_event",
        "health_update",
        "intention_event",
        "open_path",
    ]
