# Regression: nothing on a request path may hold the event loop for the SUM of
# its recipients, and no stream may live forever.
#
# Created 2026-09-04 (backend-perf H1, M7). PocketPaw runs ONE uvicorn process
# with no --workers, so a coroutine that awaits serially over N recipients does
# not just make itself slow — it is the only thing the box is doing for that
# whole time.
#
#   H1  Every realtime fan-out was `for x in recipients: await send(x)`, running
#       inline in the emitting HTTP request. ws.py already had a 5s per-socket
#       timeout added for exactly this failure, but a per-send timeout bounds
#       one send; the loop around it re-summed them. 200 members with ten
#       back-pressured tabs = ~50s holding the sender's POST, with every
#       individual timeout behaving perfectly.
#   M7  The SSE run stream had no maximum lifetime. Its only exits were a
#       terminal event or a failed yield, so a run that never wrote its terminal
#       frame heartbeated forever.
#
# What each test would catch — see the repo's mutation rule, and
# tests/mutations/event_loop.json for the mutations actually run:
#   - revert bus.publish to a serial for-loop        -> TestBusFanOut
#   - revert send_to_user's gather to a serial loop  -> TestConnectionManagerFanOut
#   - drop the semaphore from map_bounded            -> test_concurrency_is_capped
#   - delete the SSE deadline check                  -> TestStreamLifetime
#   - make the stream cap a constant, not derived    -> test_cap_follows_job_timeout
#
# NOTE ON MEASURING CONCURRENCY: these tests assert on OBSERVED OVERLAP (how
# many sends were in flight at once) and on ordering, never on wall-clock
# duration. A timing assertion would be flaky on Windows, whose monotonic clock
# ticks at ~15.6ms — the same granularity that already broke a sweep test in
# this suite when it used max_age=0.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud._core.realtime.fanout import FANOUT_CONCURRENCY, map_bounded
from pocketpaw_ee.cloud.chat.runs import domain as runs_domain


class _Tracker:
    """Records maximum observed overlap across calls."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.order: list[str] = []

    async def call(self, key: str, gate: asyncio.Event | None = None) -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if gate is not None:
                await gate.wait()
            else:
                await asyncio.sleep(0)
            self.order.append(key)
            return key
        finally:
            self.in_flight -= 1


class TestMapBounded:
    @pytest.mark.asyncio
    async def test_calls_run_concurrently(self):
        t = _Tracker()
        gate = asyncio.Event()

        async def _run():
            return await map_bounded([f"u{i}" for i in range(10)], lambda k: t.call(k, gate))

        task = asyncio.create_task(_run())
        # Let every coroutine reach the gate before releasing it. If the fan-out
        # were serial only ONE would be waiting, because the second could not
        # start until the first returned.
        for _ in range(10):
            await asyncio.sleep(0)
        assert t.in_flight == 10, f"only {t.in_flight} sends were in flight; fan-out is serial"
        gate.set()
        assert await task == [f"u{i}" for i in range(10)]

    @pytest.mark.asyncio
    async def test_concurrency_is_capped(self):
        """Unbounded gather would trade a latency problem for a task-count one."""
        t = _Tracker()
        gate = asyncio.Event()
        n = FANOUT_CONCURRENCY * 3

        async def _run():
            return await map_bounded([f"u{i}" for i in range(n)], lambda k: t.call(k, gate))

        task = asyncio.create_task(_run())
        for _ in range(n + 5):
            await asyncio.sleep(0)
        assert t.max_in_flight == FANOUT_CONCURRENCY, (
            f"{t.max_in_flight} sends in flight, expected the cap of {FANOUT_CONCURRENCY}"
        )
        gate.set()
        await task

    @pytest.mark.asyncio
    async def test_results_stay_positional(self):
        """Callers zip results against their input to attribute outcomes, so
        completion order must not reorder them."""
        delays = {"a": 0.03, "b": 0.0, "c": 0.015}

        async def _slow(key: str) -> str:
            await asyncio.sleep(delays[key])
            return key.upper()

        assert await map_bounded(["a", "b", "c"], _slow) == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_empty_input_is_a_no_op(self):
        assert await map_bounded([], lambda _: asyncio.sleep(0)) == []

    @pytest.mark.asyncio
    async def test_an_exception_propagates_instead_of_becoming_a_result(self):
        """Do NOT reach for gather(return_exceptions=True) here.

        Callers zip the returned list against their input and read each entry as
        the outcome for that recipient. ``send_to_user`` reads it as a bool: an
        exception OBJECT sitting in that list is truthy, so a socket that raised
        would be counted as DELIVERED. That count is the signal
        ``push/dispatch.py`` uses to skip Web Push, so the notification would be
        dropped on the floor and every layer would report success.

        Callers that want containment wrap their own coroutine — see
        ``InProcessBus.publish``.
        """

        async def _boom(key: str) -> str:
            if key == "bad":
                raise RuntimeError("socket exploded")
            return key

        with pytest.raises(RuntimeError, match="socket exploded"):
            await map_bounded(["ok", "bad"], _boom)


class _StubConn:
    """Stands in for ConnectionManager. Blocks until released, like a socket
    that is not being read."""

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate
        self.in_flight = 0
        self.max_in_flight = 0
        self.sent: list[str] = []

    async def send_to_user(self, uid: str, payload) -> int:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self.gate.wait()
            self.sent.append(uid)
            return 1
        finally:
            self.in_flight -= 1


class _StubResolver:
    def __init__(self, audience: list[str]) -> None:
        self._audience = audience

    async def audience(self, event) -> list[str]:
        return self._audience


class _Event:
    type = "message.new"
    data = {"hello": "world"}


class TestBusFanOut:
    """publish() runs INLINE in the emitting HTTP request, which is why the
    serial loop was charged to the sender."""

    @pytest.mark.asyncio
    async def test_publish_fans_out_concurrently(self):
        gate = asyncio.Event()
        conn = _StubConn(gate)
        audience = [f"u{i}" for i in range(12)]
        bus = InProcessBus(resolver=_StubResolver(audience), conn_manager=conn)

        task = asyncio.create_task(bus.publish(_Event()))
        for _ in range(20):
            await asyncio.sleep(0)
        assert conn.max_in_flight == 12, (
            f"{conn.max_in_flight} of 12 recipients were in flight; "
            "publish is still serial and charges the sender the sum"
        )
        gate.set()
        await task
        assert sorted(conn.sent) == sorted(audience)

    @pytest.mark.asyncio
    async def test_one_failing_recipient_does_not_abort_the_rest(self):
        """Containment was per-recipient in the serial loop and must stay so."""
        delivered: list[str] = []

        class _Flaky:
            async def send_to_user(self, uid: str, payload) -> int:
                if uid == "bad":
                    raise RuntimeError("socket exploded")
                delivered.append(uid)
                return 1

        bus = InProcessBus(resolver=_StubResolver(["a", "bad", "b"]), conn_manager=_Flaky())
        await bus.publish(_Event())
        assert sorted(delivered) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_local_handlers_still_run(self):
        """The fan-out change must not disturb in-process subscribers."""
        seen: list[str] = []

        class _NoConn:
            async def send_to_user(self, uid: str, payload) -> int:
                return 1

        bus = InProcessBus(resolver=_StubResolver([]), conn_manager=_NoConn())

        async def _handler(event):
            seen.append(event.type)

        bus.subscribe("message.new", _handler)
        await bus.publish(_Event())
        assert seen == ["message.new"]


class _FakeWS:
    def __init__(self, name: str, gate: asyncio.Event | None = None) -> None:
        self.name = name
        self.gate = gate
        self.received: list[dict] = []
        # Tracks whether this socket's send has STARTED, which is the only
        # thing that separates concurrent from serial here. Asserting on
        # "hasn't finished" instead is vacuous: under a gate that nothing has
        # released, no socket has finished in EITHER arrangement. That version
        # of this test passed the serial mutation.
        self.in_flight = False

    async def send_json(self, data: dict) -> None:
        self.in_flight = True
        try:
            if self.gate is not None:
                await self.gate.wait()
            self.received.append(data)
        finally:
            self.in_flight = False

    async def close(self, *a, **kw) -> None:  # pragma: no cover - bookkeeping only
        pass


class TestConnectionManagerFanOut:
    """One user's sockets are their open tabs and devices. Serially, one
    back-pressured tab cost every other device the full 5s before its own frame
    was even attempted."""

    @pytest.mark.asyncio
    async def test_send_to_user_sends_to_all_sockets_at_once(self):
        from pocketpaw_ee.cloud.chat import ws as ws_mod

        gate = asyncio.Event()
        cm = ws_mod.ConnectionManager()
        socks = [_FakeWS(f"s{i}", gate) for i in range(4)]
        cm.active_connections["u1"] = set(socks)
        for s in socks:
            cm._ws_to_user[s] = "u1"

        class _Msg:
            def model_dump(self, mode="json"):
                return {"type": "x"}

        task = asyncio.create_task(cm.send_to_user("u1", _Msg()))
        for _ in range(10):
            await asyncio.sleep(0)
        started = sum(1 for s in socks if s.in_flight)
        assert started == 4, (
            f"only {started} of 4 sockets had started; they are being written one at a time"
        )
        gate.set()
        assert await task == 4
        assert all(s.received for s in socks)

    @pytest.mark.asyncio
    async def test_delivered_count_survives_a_dead_socket(self):
        """``delivered`` is the signal push/dispatch.py reads to choose WS over
        Web Push. Getting it wrong silently eats notifications."""
        from pocketpaw_ee.cloud.chat import ws as ws_mod

        good = _FakeWS("good")
        bad = _FakeWS("bad")

        async def _explode(data):
            raise RuntimeError("broken pipe")

        bad.send_json = _explode

        cm = ws_mod.ConnectionManager()
        cm.active_connections["u1"] = {good, bad}
        cm._ws_to_user[good] = "u1"
        cm._ws_to_user[bad] = "u1"

        class _Msg:
            def model_dump(self, mode="json"):
                return {"type": "x"}

        assert await cm.send_to_user("u1", _Msg()) == 1
        assert good.received == [{"type": "x"}]


class _StubTransport:
    """read_events yields nothing and returns, which is what a stream with no
    new entries does after its block window expires."""

    def __init__(self) -> None:
        self.reads = 0

    async def stream_exists(self, run_id: str) -> bool:
        return True

    async def read_events(self, run_id: str, *, after: str = "0", block_ms: int = 15000):
        self.reads += 1
        await asyncio.sleep(0)
        return
        yield  # pragma: no cover - makes this an async generator


class TestStreamLifetime:
    @pytest.mark.asyncio
    async def test_stream_terminates_at_the_deadline(self, monkeypatch):
        """Without the cap this loop is infinite. The test would HANG rather
        than fail on a revert, so it carries its own timeout."""
        from pocketpaw_ee.cloud.chat.runs import router as runs_router

        monkeypatch.setattr(runs_router, "stream_max_lifetime_seconds", lambda: 0)

        transport = _StubTransport()
        monkeypatch.setattr(runs_router, "get_stream_transport", lambda: transport)

        class _Doc:
            status = "running"
            assistant_message_id = "m1"
            usage: dict = {}
            workspace = "w1"
            user_id = "u1"

        async def _authorize(run_id, workspace_id, user_id):
            return _Doc()

        monkeypatch.setattr(runs_router, "_authorize", _authorize)

        response = await runs_router.get_run_stream(
            "r1", after="0", user_id="u1", workspace_id="w1"
        )
        frames = []
        async with asyncio.timeout(10):
            async for chunk in response.body_iterator:
                frames.append(chunk if isinstance(chunk, bytes) else chunk.encode())

        body = b"".join(frames)
        assert b"event: error" in body, f"stream did not terminate; got {body!r}"
        assert b"run.stream_timeout" in body
        assert transport.reads >= 1, "the deadline fired before reading anything"

    def test_cap_follows_the_job_timeout(self, monkeypatch):
        """Derived, not a second knob. A cap SHORTER than the run it watches
        severs healthy long runs and reads as a backend bug."""
        monkeypatch.setenv("POCKETPAW_CLOUD_RUN_JOB_TIMEOUT", "7200")
        assert runs_domain.run_job_timeout_seconds() == 7200
        assert runs_domain.stream_max_lifetime_seconds() > 7200

    def test_cap_is_never_shorter_than_the_run(self, monkeypatch):
        for raw in ("", "1800", "60", "not-a-number", "-1", "0"):
            monkeypatch.setenv("POCKETPAW_CLOUD_RUN_JOB_TIMEOUT", raw)
            assert (
                runs_domain.stream_max_lifetime_seconds() > runs_domain.run_job_timeout_seconds()
            ), f"cap would cut a healthy run short at POCKETPAW_CLOUD_RUN_JOB_TIMEOUT={raw!r}"

    def test_worker_and_stream_read_one_resolver(self, monkeypatch):
        """Two copies of the parse would let the cap and the run drift apart."""
        from pocketpaw_ee.cloud.chat.runs import worker as worker_mod

        assert worker_mod._job_timeout_seconds is runs_domain.run_job_timeout_seconds
        monkeypatch.setenv("POCKETPAW_CLOUD_RUN_JOB_TIMEOUT", "2400")
        assert worker_mod._job_timeout_seconds() == 2400
        assert runs_domain.stream_max_lifetime_seconds() > 2400
