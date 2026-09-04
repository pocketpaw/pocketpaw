# Regression: the worker→web bridge dispatches concurrently, but only across
# conversations — never within one.
#
# Created 2026-09-04 (backend-perf H2). Every worker-originated realtime frame
# in the deployment flows through one consumer loop, which awaited each
# envelope in turn. A single slow dispatch therefore stalled delivery for every
# tenant on the box: one workspace with a back-pressured socket made agent
# replies look frozen for every other customer.
#
# THE TRAP THIS FILE EXISTS FOR: a plain gather over the batch removes the
# stall and introduces a worse bug. These envelopes carry streamed agent
# output, so the chunks of one reply must arrive in the order they were
# produced. Parallelising within a conversation renders the answer scrambled —
# and nothing raises, no status code changes, and a test that only measures
# concurrency reports success.
#
# So this file asserts BOTH directions. Every "runs concurrently" test has a
# matching "stays ordered" test, and neither alone is sufficient.
#
# What each test would catch (mutations in tests/mutations/xproc_lanes.json):
#   - go back to a fully serial loop      -> test_separate_lanes_run_concurrently
#   - gather the whole batch flat         -> test_one_lane_stays_in_order
#   - key the lane on the entry id        -> test_one_lane_stays_in_order
#   - drop the ack on a failed dispatch   -> test_a_failing_dispatch_is_still_acked
#   - let an unparseable envelope raise   -> test_an_unparseable_envelope_is_acked

from __future__ import annotations

import asyncio
import json

import pytest
from pocketpaw_ee.cloud._core.realtime import xproc


def _entry(entry_id: str, envelope: dict) -> tuple[str, dict]:
    return (entry_id, {"envelope": json.dumps(envelope)})


def _ws(scope_id: str, seq: int) -> dict:
    return {
        "kind": "ws",
        "scope_id": scope_id,
        "recipients": ["u1"],
        "type": "message.chunk",
        "data": {"seq": seq},
    }


class _FakeRedis:
    """Serves one batch, then blocks so the consumer loop parks."""

    def __init__(self, batch: list[tuple[str, dict]]) -> None:
        self._batch = batch
        self._served = False
        self.acked: list[str] = []

    async def xgroup_create(self, *a, **kw) -> None:
        return None

    async def xreadgroup(self, *a, **kw):
        if self._served:
            await asyncio.sleep(3600)
        self._served = True
        return [(xproc.XPROC_STREAM, self._batch)]

    async def xack(self, _stream, _group, entry_id) -> None:
        self.acked.append(entry_id)


async def _consume_one_batch(monkeypatch, batch, dispatch):
    """Run the consumer just long enough to process one batch."""
    redis = _FakeRedis(batch)
    monkeypatch.setattr(xproc, "get_redis", lambda: redis)
    monkeypatch.setattr(xproc, "_dispatch", dispatch)

    task = asyncio.create_task(xproc.run_consumer(consumer_name="test", block_ms=1))
    for _ in range(200):
        await asyncio.sleep(0)
        if len(redis.acked) >= len(batch):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return redis


class TestLaneConcurrency:
    async def test_separate_lanes_run_concurrently(self, monkeypatch):
        """Different conversations must not queue behind each other. This is
        the finding: one wedged workspace froze every tenant."""
        gate = asyncio.Event()
        in_flight = 0
        peak = 0

        async def _dispatch(envelope):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await gate.wait()
            finally:
                in_flight -= 1

        batch = [_entry(f"e{i}", _ws(f"scope-{i}", 0)) for i in range(6)]
        redis = _FakeRedis(batch)
        monkeypatch.setattr(xproc, "get_redis", lambda: redis)
        monkeypatch.setattr(xproc, "_dispatch", _dispatch)

        task = asyncio.create_task(xproc.run_consumer(consumer_name="t", block_ms=1))
        for _ in range(50):
            await asyncio.sleep(0)

        assert peak == 6, f"only {peak} of 6 lanes were in flight; dispatch is still serial"
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0)
            if len(redis.acked) >= 6:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_one_lane_stays_in_order(self, monkeypatch):
        """The chunks of a single reply. Out of order here is a scrambled
        answer on someone's screen, with nothing raised anywhere.

        Durations DESCEND deliberately. Equal durations are not enough: tasks
        started in order that yield the same number of times finish in order
        too, so a flat gather looks correct. Making the first envelope the
        slowest means anything concurrent completes roughly backwards, and the
        assertion can tell the two apart.
        """
        seen: list[int] = []
        count = 6

        async def _dispatch(envelope):
            seq = envelope["data"]["seq"]
            await asyncio.sleep((count - seq) * 0.01)
            seen.append(seq)

        batch = [_entry(f"e{i}", _ws("scope-A", i)) for i in range(count)]
        redis = _FakeRedis(batch)
        monkeypatch.setattr(xproc, "get_redis", lambda: redis)
        monkeypatch.setattr(xproc, "_dispatch", _dispatch)

        task = asyncio.create_task(xproc.run_consumer(consumer_name="t", block_ms=1))
        async with asyncio.timeout(10):
            while len(redis.acked) < count:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert seen == list(range(count)), f"chunks arrived out of order: {seen}"

    async def test_interleaved_lanes_keep_their_own_order(self, monkeypatch):
        """Two conversations at once: each stays internally ordered."""
        per_lane: dict[str, list[int]] = {}

        async def _dispatch(envelope):
            await asyncio.sleep(0)
            per_lane.setdefault(envelope["scope_id"], []).append(envelope["data"]["seq"])

        batch = []
        for i in range(5):
            batch.append(_entry(f"a{i}", _ws("scope-A", i)))
            batch.append(_entry(f"b{i}", _ws("scope-B", i)))
        await _consume_one_batch(monkeypatch, batch, _dispatch)

        assert per_lane["scope-A"] == list(range(5))
        assert per_lane["scope-B"] == list(range(5))


class TestAcking:
    async def test_every_entry_is_acked(self, monkeypatch):
        async def _dispatch(envelope):
            return None

        batch = [_entry(f"e{i}", _ws(f"s{i}", 0)) for i in range(4)]
        redis = await _consume_one_batch(monkeypatch, batch, _dispatch)

        assert sorted(redis.acked) == ["e0", "e1", "e2", "e3"]

    async def test_a_failing_dispatch_is_still_acked(self, monkeypatch):
        """An unacked entry is redelivered forever and stalls the stream. The
        original loop acked in a finally; concurrency must not lose that."""

        async def _dispatch(envelope):
            raise RuntimeError("handler exploded")

        batch = [_entry("e0", _ws("s0", 0))]
        redis = await _consume_one_batch(monkeypatch, batch, _dispatch)

        assert redis.acked == ["e0"]

    async def test_a_failing_lane_does_not_stop_the_others(self, monkeypatch):
        delivered: list[str] = []

        async def _dispatch(envelope):
            if envelope["scope_id"] == "bad":
                raise RuntimeError("boom")
            delivered.append(envelope["scope_id"])

        batch = [
            _entry("e0", _ws("good-1", 0)),
            _entry("e1", _ws("bad", 0)),
            _entry("e2", _ws("good-2", 0)),
        ]
        redis = await _consume_one_batch(monkeypatch, batch, _dispatch)

        assert sorted(delivered) == ["good-1", "good-2"]
        assert sorted(redis.acked) == ["e0", "e1", "e2"]

    async def test_an_unparseable_envelope_is_acked_and_isolated(self, monkeypatch):
        """A malformed entry must not stall the stream, and must not be able
        to delay a real one by sharing its lane."""
        delivered: list[str] = []

        async def _dispatch(envelope):
            delivered.append(envelope["scope_id"])

        batch = [
            ("bad", {"envelope": "{not json"}),
            _entry("e1", _ws("s1", 0)),
        ]
        redis = await _consume_one_batch(monkeypatch, batch, _dispatch)

        assert delivered == ["s1"]
        assert sorted(redis.acked) == ["bad", "e1"]


class TestOrderingLane:
    def test_ws_envelopes_key_on_scope(self):
        assert xproc._ordering_lane(_ws("scope-A", 0)) == ("ws", "scope-A")

    def test_bus_envelopes_key_on_a_scope_field_in_data(self):
        env = {"kind": "bus", "type": "message.new", "data": {"group_id": "g1"}}
        assert xproc._ordering_lane(env) == ("bus", "g1")

    def test_a_bus_envelope_with_no_scope_falls_back_to_its_type(self):
        """Conservative on purpose. Falling back to something UNIQUE would
        parallelise a stream whose ordering needs we could not read, and the
        symptom would be a scrambled reply rather than an error."""
        env = {"kind": "bus", "type": "workspace.updated", "data": {"nothing": 1}}
        assert xproc._ordering_lane(env) == ("bus-type", "workspace.updated")

    def test_two_envelopes_of_the_same_unscoped_type_share_a_lane(self):
        a = {"kind": "bus", "type": "x.y", "data": {}}
        b = {"kind": "bus", "type": "x.y", "data": {}}
        assert xproc._ordering_lane(a) == xproc._ordering_lane(b)

    def test_unknown_kinds_share_one_lane(self):
        env = {"kind": "from-a-newer-worker", "type": "?"}
        assert xproc._ordering_lane(env) == ("unknown", "from-a-newer-worker")

    def test_a_non_dict_data_does_not_raise(self):
        env = {"kind": "bus", "type": "x.y", "data": "not-a-dict"}
        assert xproc._ordering_lane(env) == ("bus-type", "x.y")
