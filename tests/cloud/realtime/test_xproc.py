"""Cross-process bus/WS bridge for Tier 2 resumable runs.

The worker process can't reach the web process's InProcessBus or WS manager.
``xproc`` ships envelopes through a Redis stream; the consumer on the web side
rebuilds the original Event subclass via ``EVENT_REGISTRY`` and dispatches to
``bus.publish`` / ``manager.broadcast_to_group``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud._core.realtime.events import GroupCreated, MessageNew

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_redis(monkeypatch):
    """Swap the singleton Redis client for fakeredis so every xproc primitive
    talks to an in-memory store."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(xproc, "get_redis", lambda: redis)
    xproc._reset_for_tests()
    yield redis
    xproc._reset_for_tests()


# --- role + publish primitives ----------------------------------------------


async def test_default_role_is_web():
    xproc._reset_for_tests()
    assert xproc.is_worker() is False


async def test_set_role_worker_flips_flag():
    xproc._reset_for_tests()
    xproc.set_role("worker")
    assert xproc.is_worker() is True


async def test_publish_bus_envelope_noop_when_role_is_web(fake_redis):
    await xproc.publish_bus_envelope(GroupCreated(data={"id": "g1"}))
    # The web side must never publish; if it did the message would loop back
    # through its own consumer and get double-dispatched.
    assert await fake_redis.exists(xproc.XPROC_STREAM) == 0


async def test_publish_bus_envelope_in_worker_writes_to_stream(fake_redis):
    xproc.set_role("worker")
    evt = GroupCreated(data={"id": "g1", "name": "team"})

    await xproc.publish_bus_envelope(evt)

    entries = await fake_redis.xrange(xproc.XPROC_STREAM)
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    envelope = json.loads(fields["envelope"])
    assert envelope["kind"] == "bus"
    assert envelope["type"] == "group.created"
    assert envelope["data"] == {"id": "g1", "name": "team"}
    assert envelope["ts"]  # iso string present


async def test_publish_ws_envelope_in_worker_writes_to_stream(fake_redis):
    xproc.set_role("worker")

    await xproc.publish_ws_envelope(
        scope_id="s1",
        recipients=["u1", "u2"],
        ws_type="agent.typing",
        ws_data={"scope": "session", "scope_id": "s1", "agent_id": "a1", "active": True},
    )

    entries = await fake_redis.xrange(xproc.XPROC_STREAM)
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    envelope = json.loads(fields["envelope"])
    assert envelope == {
        "kind": "ws",
        "scope_id": "s1",
        "recipients": ["u1", "u2"],
        "type": "agent.typing",
        "data": {"scope": "session", "scope_id": "s1", "agent_id": "a1", "active": True},
    }


async def test_publish_ws_envelope_noop_when_role_is_web(fake_redis):
    await xproc.publish_ws_envelope(scope_id="s1", recipients=["u1"], ws_type="x", ws_data={})
    assert await fake_redis.exists(xproc.XPROC_STREAM) == 0


# --- consumer dispatch ------------------------------------------------------


@pytest.fixture
def fake_bus(monkeypatch):
    bus = AsyncMock()
    bus.publish = AsyncMock()
    monkeypatch.setattr(xproc, "get_bus", lambda: bus)
    return bus


@pytest.fixture
def fake_manager(monkeypatch):
    """Replace the WS manager singleton with an AsyncMock so dispatch is
    observable without spinning up a real WebSocket."""
    import pocketpaw_ee.cloud.chat.ws as ws_mod

    fake = AsyncMock()
    fake.broadcast_to_group = AsyncMock()
    monkeypatch.setattr(ws_mod, "manager", fake)
    return fake


async def test_dispatch_bus_envelope_publishes_to_local_bus(fake_bus):
    envelope = {
        "kind": "bus",
        "type": "message.new",
        "data": {"id": "m1", "group": "g1"},
        "ts": "2026-05-23T19:00:00+00:00",
    }

    await xproc._dispatch(envelope)

    fake_bus.publish.assert_awaited_once()
    event = fake_bus.publish.await_args.args[0]
    assert isinstance(event, MessageNew)
    assert event.data == {"id": "m1", "group": "g1"}


async def test_dispatch_ws_envelope_calls_manager(fake_manager):
    envelope = {
        "kind": "ws",
        "scope_id": "s1",
        "recipients": ["u1", "u2"],
        "type": "agent.typing",
        "data": {"active": True},
    }

    await xproc._dispatch(envelope)

    fake_manager.broadcast_to_group.assert_awaited_once()
    args = fake_manager.broadcast_to_group.await_args.args
    assert args[0] == "s1"
    assert args[1] == ["u1", "u2"]
    ws_out = args[2]
    assert ws_out.type == "agent.typing"
    assert ws_out.data == {"active": True}


async def test_dispatch_unknown_kind_does_not_raise(fake_bus, fake_manager):
    await xproc._dispatch({"kind": "future", "x": 1})
    # Forward-compatible: an older consumer should skip envelopes it doesn't
    # understand, not crash and stall the whole stream.
    fake_bus.publish.assert_not_called()
    fake_manager.broadcast_to_group.assert_not_called()


# --- consumer loop integration ---------------------------------------------


async def test_run_consumer_dispatches_published_envelope(
    fake_redis,
    fake_bus,
    fake_manager,
):
    """End-to-end through the loop: web starts the consumer first (matches
    production order), worker publishes, consumer drains + dispatches, then
    cancellation cleanly stops the loop."""
    # Web side starts the consumer first so the group exists with a cursor
    # at the current tip; subsequent worker publishes flow through.
    task = asyncio.create_task(xproc.run_consumer(consumer_name="test", block_ms=50))
    await _wait_until_group_exists(fake_redis)

    xproc.set_role("worker")
    await xproc.publish_bus_envelope(GroupCreated(data={"id": "g1"}))
    await xproc.publish_ws_envelope(
        scope_id="s1", recipients=["u1"], ws_type="agent.typing", ws_data={"active": True}
    )

    await _wait_until(
        lambda: (
            fake_bus.publish.await_count == 1 and fake_manager.broadcast_to_group.await_count == 1
        ),
        timeout=2.0,
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_consumer_acks_so_restart_does_not_replay(
    fake_redis,
    fake_bus,
    fake_manager,
):
    """Consumer-group XACK semantics: after the consumer processes an entry,
    a restart (new consumer in the same group) must not redeliver it."""
    task1 = asyncio.create_task(xproc.run_consumer(consumer_name="c1", block_ms=50))
    await _wait_until_group_exists(fake_redis)

    xproc.set_role("worker")
    await xproc.publish_bus_envelope(GroupCreated(data={"id": "g1"}))

    await _wait_until(lambda: fake_bus.publish.await_count == 1, timeout=2.0)
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1

    # New consumer in the same group: nothing to deliver.
    task2 = asyncio.create_task(xproc.run_consumer(consumer_name="c2", block_ms=50))
    await asyncio.sleep(0.2)  # give it a chance to (not) re-deliver
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2

    assert fake_bus.publish.await_count == 1


async def test_run_consumer_idempotent_on_group_create(fake_redis):
    """The consumer creates the consumer group on first run with
    mkstream=True; a second start must not crash on BUSYGROUP."""
    task1 = asyncio.create_task(xproc.run_consumer(consumer_name="c1", block_ms=50))
    await asyncio.sleep(0.05)
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1

    task2 = asyncio.create_task(xproc.run_consumer(consumer_name="c2", block_ms=50))
    await asyncio.sleep(0.05)
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2


# --- helpers ----------------------------------------------------------------


async def _wait_until(predicate, *, timeout: float) -> None:
    """Poll a predicate until it returns truthy or the timeout elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for predicate")


async def _wait_until_group_exists(redis, *, timeout: float = 2.0) -> None:
    """Wait until the consumer task has created the consumer group. Publishing
    before the group exists means the entry lands at ``$`` and is missed by
    the new group's initial cursor."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            groups = await redis.xinfo_groups(xproc.XPROC_STREAM)
            if any(g.get("name") == xproc.XPROC_GROUP for g in groups):
                return
        except Exception:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError("consumer group never appeared")


async def test_bus_publish_failure_does_not_crash_consumer(fake_redis, fake_manager, monkeypatch):
    """A misbehaving listener (rebuild_event raising, bus.publish raising)
    must not stall the stream — the bad entry is acked and the loop continues."""
    publish_calls: list[Any] = []

    class _Bus:
        async def publish(self, event):
            publish_calls.append(event)
            if len(publish_calls) == 1:
                raise RuntimeError("listener boom")

    monkeypatch.setattr(xproc, "get_bus", lambda: _Bus())

    task = asyncio.create_task(xproc.run_consumer(consumer_name="c1", block_ms=50))
    await _wait_until_group_exists(fake_redis)

    xproc.set_role("worker")
    await xproc.publish_bus_envelope(GroupCreated(data={"id": "g1"}))
    await xproc.publish_bus_envelope(GroupCreated(data={"id": "g2"}))

    await _wait_until(lambda: len(publish_calls) == 2, timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(publish_calls) == 2
