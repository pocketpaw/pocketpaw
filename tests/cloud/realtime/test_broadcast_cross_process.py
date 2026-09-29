"""Cross-process realtime delivery, with two web "processes" in one test.

Each simulated process is its own ``ConnectionManager`` + broadcast ``Channel``
(its own consumer group) + ``InProcessBus``, all pointed at the real local
Redis with a unique stream per test. What is pinned:

- a frame published on A reaches B's sockets exactly once, and A does not
  deliver its own frame twice (the origin skip);
- ``broadcast_to_group`` / ``send_to_room`` called directly (Tier-1 agent
  replies, typing, read receipts) cross processes the same way;
- a worker ws envelope reaches both processes, a worker bus envelope runs its
  handler exactly once across both (the shared ``xproc`` group);
- dead processes' groups are reaped, fresh ones are not; a process whose group
  vanished rejoins;
- ``cache.invalidate`` runs on every process, and the two security caches are
  wired to it;
- with broadcast off nothing touches Redis.

Mutations: ``tests/mutations/realtime_cross_process.json``. Skipped when no
Redis answers on localhost:6379 (CI has no Redis service).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from pocketpaw_ee.cloud._core.realtime import broadcast, xproc
from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
from pocketpaw_ee.cloud._core.realtime.broadcast import Channel
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud._core.realtime.events import GroupCreated
from pocketpaw_ee.cloud.chat.schemas import WsOutbound
from pocketpaw_ee.cloud.chat.ws import ConnectionManager

pytestmark = pytest.mark.asyncio

BLOCK_MS = 100


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("no Redis on localhost:6379")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def stream(redis):
    name = f"test:realtime:broadcast:{uuid.uuid4().hex}"
    yield name
    await redis.delete(name)


@pytest.fixture(autouse=True)
def _reset():
    broadcast._reset_for_tests()
    xproc._reset_for_tests()
    yield
    broadcast._reset_for_tests()
    xproc._reset_for_tests()


class _Resolver:
    def __init__(self, audience: list[str]) -> None:
        self._audience = audience

    async def audience(self, event):  # noqa: ARG002
        return list(self._audience)


class _Proc:
    """One simulated web process."""

    def __init__(self, redis, stream: str, audience: list[str], **channel_kw) -> None:
        self.cm = ConnectionManager()
        self.channel = Channel(
            redis, stream=stream, conn_manager=self.cm, block_ms=BLOCK_MS, **channel_kw
        )
        self.cm.relay_channel = self.channel
        self.bus = InProcessBus(
            resolver=_Resolver(audience), conn_manager=self.cm, channel=self.channel
        )

    async def socket(self, user_id: str) -> AsyncMock:
        ws = AsyncMock()
        await self.cm.connect(ws, user_id)
        return ws


@pytest_asyncio.fixture
async def procs(redis, stream):
    made: list[_Proc] = []

    async def _make(audience=(), **kw) -> _Proc:
        proc = _Proc(redis, stream, list(audience), **kw)
        await proc.channel.start()
        made.append(proc)
        return proc

    yield _make
    for proc in made:
        await proc.channel.stop()


def _frames(ws: AsyncMock) -> list[dict]:
    return [c.args[0] for c in ws.send_json.await_args_list]


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for predicate")


async def _settle() -> None:
    # Several read cycles, so a duplicate or looping frame has time to land.
    await asyncio.sleep(BLOCK_MS / 1000 * 4)


# --- web-originated frames ----------------------------------------------------


async def test_a_bus_frame_on_a_reaches_b_exactly_once_and_a_once(procs):
    a = await procs(audience=["u1", "u2"])
    b = await procs(audience=["u1", "u2"])
    ws_a_u1 = await a.socket("u1")
    ws_b_u1 = await b.socket("u1")  # the same user's second tab, on another process
    ws_b_u2 = await b.socket("u2")

    await a.bus.publish(GroupCreated(data={"group_id": "g1", "name": "x"}))

    await _until(lambda: _frames(ws_b_u1) and _frames(ws_b_u2))
    await _settle()
    assert [f["type"] for f in _frames(ws_a_u1)] == ["group.created"]
    assert [f["type"] for f in _frames(ws_b_u1)] == ["group.created"]
    assert [f["type"] for f in _frames(ws_b_u2)] == ["group.created"]
    assert _frames(ws_b_u2)[0]["data"] == {"group_id": "g1", "name": "x"}


async def test_bus_handlers_run_only_on_the_publishing_process(procs):
    a = await procs(audience=["u1"])
    b = await procs(audience=["u1"])
    await b.socket("u1")
    ran: list[str] = []

    async def _on_a(_e):
        ran.append("a")

    async def _on_b(_e):
        ran.append("b")

    a.bus.subscribe("group.created", _on_a)
    b.bus.subscribe("group.created", _on_b)
    ws = await b.socket("u1")

    await a.bus.publish(GroupCreated(data={"group_id": "g1"}))
    await _until(lambda: _frames(ws))
    await _settle()
    assert ran == ["a"]


async def test_broadcast_to_group_crosses_processes(procs):
    """The path Tier-1 agent replies take (run_core → manager.broadcast_to_group)."""
    a = await procs()
    b = await procs()
    ws_a = await a.socket("u1")
    ws_b = await b.socket("u2")

    await a.cm.broadcast_to_group("g1", ["u1", "u2"], WsOutbound(type="message.new", data={"n": 1}))

    await _until(lambda: _frames(ws_b))
    await _settle()
    assert len(_frames(ws_a)) == 1
    assert len(_frames(ws_b)) == 1


async def test_send_to_room_crosses_processes_and_keeps_the_exclusion(procs):
    a = await procs()
    b = await procs()
    ws_a = await a.socket("typist")
    a.cm.join_room(ws_a, "g1")
    ws_b_typist = await b.socket("typist")  # the typist's other tab
    b.cm.join_room(ws_b_typist, "g1")
    ws_b_peer = await b.socket("peer")
    b.cm.join_room(ws_b_peer, "g1")

    await a.cm.send_to_room(
        "g1", WsOutbound(type="typing.start", data={"group_id": "g1"}), exclude_user="typist"
    )

    await _until(lambda: _frames(ws_b_peer))
    await _settle()
    assert len(_frames(ws_b_peer)) == 1
    assert _frames(ws_b_typist) == []
    assert _frames(ws_a) == []


async def test_one_scopes_frames_arrive_in_order_on_the_other_process(procs):
    a = await procs()
    b = await procs()
    ws_b = await b.socket("u2")

    for i in range(20):
        await a.cm.broadcast_to_group("g1", ["u2"], WsOutbound(type="chunk", data={"i": i}))

    await _until(lambda: len(_frames(ws_b)) == 20)
    assert [f["data"]["i"] for f in _frames(ws_b)] == list(range(20))


# --- worker-originated envelopes ----------------------------------------------


async def test_a_worker_ws_envelope_reaches_both_processes(procs, redis, stream, monkeypatch):
    a = await procs()
    b = await procs()
    ws_a = await a.socket("u1")
    ws_b = await b.socket("u2")
    # The worker's own channel: never started, it only publishes.
    broadcast.configure(enabled=True)
    monkeypatch.setattr(broadcast, "_channel", Channel(redis, stream=stream))
    # If the envelope went to the shared xproc stream instead, no consumer reads
    # it here, so the assertion below fails rather than hitting a real stream.
    monkeypatch.setattr(xproc, "XPROC_STREAM", f"{stream}:xproc")
    monkeypatch.setattr(xproc, "get_redis", lambda: redis)
    xproc.set_role("worker")

    await xproc.publish_ws_envelope(
        scope_id="g1", recipients=["u1", "u2"], ws_type="message.new", ws_data={"k": 1}
    )

    await _until(lambda: _frames(ws_a) and _frames(ws_b))
    await _settle()
    assert len(_frames(ws_a)) == 1
    assert len(_frames(ws_b)) == 1
    await redis.delete(f"{stream}:xproc")


async def test_a_worker_bus_event_is_handled_once_across_two_web_processes(
    redis, stream, monkeypatch
):
    xstream = f"{stream}:xproc"
    monkeypatch.setattr(xproc, "XPROC_STREAM", xstream)
    monkeypatch.setattr(xproc, "get_redis", lambda: redis)
    handled: list[str] = []

    async def _handler(event):
        handled.append(event.data["group_id"])

    shared_bus = InProcessBus(resolver=_Resolver([]), conn_manager=ConnectionManager())
    shared_bus.subscribe("group.created", _handler)
    monkeypatch.setattr(bus_mod, "_bus", shared_bus)

    tasks = [
        asyncio.create_task(xproc.run_consumer(consumer_name=name, block_ms=BLOCK_MS))
        for name in ("web-a", "web-b")
    ]
    try:

        async def _both_joined() -> bool:
            try:
                return len(await redis.xinfo_consumers(xstream, xproc.XPROC_GROUP)) == 2
            except Exception:
                return False

        deadline = time.monotonic() + 3
        while not await _both_joined():
            assert time.monotonic() < deadline, "both consumers never joined"
            await asyncio.sleep(0.02)

        xproc.set_role("worker")
        for i in range(10):
            await xproc.publish_bus_envelope(GroupCreated(data={"group_id": f"g{i}"}))

        await _until(lambda: len(handled) >= 10)
        await _settle()
        assert sorted(handled) == sorted(f"g{i}" for i in range(10))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.delete(xstream)


# --- group lifecycle ----------------------------------------------------------


async def test_cleanup_reaps_dead_groups_and_spares_live_and_fresh_ones(procs, redis, stream):
    live = await procs()
    reaper = Channel(redis, stream=stream, stale_ms=500)
    await reaper._create_group()
    now_ms = int(time.time() * 1000)

    dead = f"web-{now_ms - 20 * 60 * 1000}-deadbeef"  # crashed before it ever read
    idle = f"web-{now_ms}-idleidle"  # read once, then went silent
    foreign = "someone-elses-group"
    for group in (dead, idle, foreign):
        await redis.xgroup_create(stream, group, id="$", mkstream=True)
    await redis.xreadgroup(idle, "c", {stream: ">"}, count=1, block=None)
    await asyncio.sleep(0.8)
    # Created just now and not read yet: the window between a sibling's
    # XGROUP CREATE and its first XREADGROUP, which cleanup must not reap.
    fresh = f"web-{int(time.time() * 1000)}-newborn0"
    await redis.xgroup_create(stream, fresh, id="$", mkstream=True)

    removed = await reaper.cleanup_stale_groups()

    names = {g["name"] for g in await redis.xinfo_groups(stream)}
    assert set(removed) == {dead, idle}
    assert dead not in names and idle not in names
    assert {fresh, foreign, live.channel.group, reaper.group} <= names


async def test_a_clean_stop_destroys_the_processs_group(redis, stream):
    channel = Channel(redis, stream=stream, block_ms=BLOCK_MS)
    await channel.start()
    assert channel.group in {g["name"] for g in await redis.xinfo_groups(stream)}

    await channel.stop()

    assert channel.group not in {g["name"] for g in await redis.xinfo_groups(stream)}


async def test_a_process_whose_group_was_reaped_rejoins(procs, redis):
    a = await procs()
    b = await procs()
    ws_b = await b.socket("u2")

    await redis.xgroup_destroy(b.channel.stream, b.channel.group)
    await asyncio.sleep(BLOCK_MS / 1000 * 3)  # b's next read hits NOGROUP and recreates

    await a.cm.broadcast_to_group("g1", ["u2"], WsOutbound(type="after", data={}))
    await _until(lambda: _frames(ws_b))


# --- cache.invalidate ---------------------------------------------------------


async def test_broadcast_invalidate_runs_once_on_every_process(procs, monkeypatch):
    name = f"test-cache-{uuid.uuid4().hex[:6]}"
    ran: list[tuple[str, str]] = []
    a = await procs()  # module registry
    await procs(invalidators={name: lambda key: ran.append(("b", key))})  # process b
    broadcast.register_invalidator(name, lambda key: ran.append(("a", key)))
    broadcast.configure(enabled=True)
    monkeypatch.setattr(broadcast, "_channel", a.channel)
    try:
        await broadcast.broadcast_invalidate(name, "k1")
        await _until(lambda: ("b", "k1") in ran)
        await _settle()
        assert sorted(ran) == [("a", "k1"), ("b", "k1")]
    finally:
        broadcast._INVALIDATORS.pop(name, None)


async def test_the_two_security_caches_are_registered():
    from pocketpaw_ee.cloud.auth import api_keys
    from pocketpaw_ee.guards import deps

    assert broadcast._INVALIDATORS["api_key"] is api_keys._invalidate_cached_key
    assert "action_overrides" in broadcast._INVALIDATORS
    assert deps._ACTION_OVERRIDE_INVALIDATOR == "action_overrides"


async def test_an_action_override_drop_is_broadcast_but_a_remote_one_is_not(monkeypatch):
    from pocketpaw_ee.guards import deps

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(deps, "broadcast_invalidate_soon", lambda n, k: sent.append((n, k)))
    deps._ACTION_OVERRIDE_CACHE[("w1", "u1")] = (time.monotonic() + 60, ["x"])
    deps._ACTION_OVERRIDE_CACHE[("w1", "u2")] = (time.monotonic() + 60, ["x"])

    deps.invalidate_action_overrides("w1", "u1")
    assert sent == [("action_overrides", json.dumps(["w1", "u1"]))]
    assert ("w1", "u1") not in deps._ACTION_OVERRIDE_CACHE

    # What another process runs on receipt: drops locally, never re-broadcasts.
    broadcast._INVALIDATORS["action_overrides"](json.dumps(["w1", None]))
    assert ("w1", "u2") not in deps._ACTION_OVERRIDE_CACHE
    assert len(sent) == 1


async def test_revoking_an_api_key_broadcasts_the_eviction(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.auth import api_keys

    sent: list[tuple[str, str, bool]] = []

    async def _record(name, key, *, local=True):
        sent.append((name, key, local))

    monkeypatch.setattr(api_keys, "broadcast_invalidate", _record)
    doc, _plaintext = await api_keys.create_api_key(
        workspace_id="w-bc", owner_user_id="u-bc", name="k", scopes=["chat.send"]
    )
    key_id = str(doc.id)

    await api_keys.revoke_api_key(key_id, "w-bc")

    assert sent == [("api_key", key_id, False)]


# --- single-process mode ------------------------------------------------------


async def test_inprocess_mode_never_builds_a_channel_or_touches_redis(monkeypatch):
    def _no_redis():
        raise AssertionError("inprocess mode must not reach Redis")

    monkeypatch.setattr(broadcast, "get_redis", _no_redis)
    cm = ConnectionManager()
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    bus = InProcessBus(resolver=_Resolver(["u1"]), conn_manager=cm)

    await bus.publish(GroupCreated(data={"group_id": "g1"}))
    await cm.broadcast_to_group("g1", ["u1"], WsOutbound(type="x", data={}))
    cm.join_room(ws, "g1")
    await cm.send_to_room("g1", WsOutbound(type="y", data={}))
    await broadcast.broadcast_invalidate("nothing-registered", "k")
    broadcast.broadcast_invalidate_soon("nothing-registered", "k")

    assert broadcast.active_channel() is None
    assert broadcast._channel is None
    assert [f["type"] for f in _frames(ws)] == ["group.created", "x", "y"]


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, False), ("inprocess", False), ("redis-streams", True), ("REDIS-STREAMS ", True)],
)
async def test_init_realtime_reads_the_knob(monkeypatch, value, enabled):
    from pocketpaw_ee.cloud import init_realtime

    if value is None:
        monkeypatch.delenv("POCKETPAW_REALTIME_BUS", raising=False)
    else:
        monkeypatch.setenv("POCKETPAW_REALTIME_BUS", value)

    init_realtime()

    assert broadcast.is_enabled() is enabled
