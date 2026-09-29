"""Cluster presence across web processes, with two "processes" in one test.

Each simulated process is its own ``ConnectionManager`` with its own presence
``Registry`` (its own process id), both on the real local Redis under a unique
key prefix. What is pinned:

- a user connected only on A is online as far as B can tell, including for the
  connect snapshot (``online_among``) and the offline grace timer;
- ``presence.online`` material (the "first connection" verdict) is true exactly
  once across processes, even when two connects race; the "last connection"
  verdict likewise;
- a crashed process's members age out; a live process's heartbeat keeps them;
- push dispatch on A delivers to a user whose socket is on B over the relay,
  not as Web Push;
- in inprocess mode nothing touches Redis, and a Redis failure falls back to
  the process-local answer.

Mutations: ``tests/mutations/multiworker_safety.json``. Skipped when no Redis
answers on localhost:6379 (CI has no Redis service).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from pocketpaw_ee.cloud._core.realtime import broadcast, presence
from pocketpaw_ee.cloud._core.realtime.broadcast import Channel
from pocketpaw_ee.cloud._core.realtime.presence import Registry
from pocketpaw_ee.cloud.chat.ws import ConnectionManager

pytestmark = pytest.mark.asyncio


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
async def prefix(redis):
    name = f"test:presence:{uuid.uuid4().hex}:"
    yield name
    keys = [k async for k in redis.scan_iter(match=f"{name}*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture(autouse=True)
def _reset():
    broadcast._reset_for_tests()
    presence._reset_for_tests()
    yield
    broadcast._reset_for_tests()
    presence._reset_for_tests()


def _proc(redis, prefix: str, **kw) -> ConnectionManager:
    cm = ConnectionManager()
    cm.presence_registry = Registry(redis, prefix=prefix, **kw)
    return cm


# --- online / first / last --------------------------------------------------


async def test_user_on_a_only_is_online_from_b(redis, prefix):
    a, b = _proc(redis, prefix), _proc(redis, prefix)

    assert await presence.connect(a, AsyncMock(), "u1") is True
    assert b.is_online("u1") is False  # B holds no socket for u1 ...
    assert await presence.is_online(b, "u1") is True  # ... but the cluster does
    assert await presence.online_among(b, ["u1", "u2"]) == {"u1"}
    assert await presence.is_online_elsewhere(b, "u1") is True
    # A's own socket is not "elsewhere" for A.
    assert await presence.is_online_elsewhere(a, "u1") is False


async def test_second_process_connect_is_not_a_first_connection(redis, prefix):
    a, b = _proc(redis, prefix), _proc(redis, prefix)

    assert await presence.connect(a, AsyncMock(), "u1") is True
    assert await presence.connect(b, AsyncMock(), "u1") is False


async def test_racing_connects_yield_exactly_one_first(redis, prefix):
    procs = [_proc(redis, prefix) for _ in range(4)]

    firsts = await asyncio.gather(*(presence.connect(p, AsyncMock(), "u1") for p in procs))

    assert sorted(firsts) == [False, False, False, True]


async def test_last_disconnect_across_processes_is_reported_once(redis, prefix):
    a, b = _proc(redis, prefix), _proc(redis, prefix)
    ws_a, ws_b = AsyncMock(), AsyncMock()
    await presence.connect(a, ws_a, "u1")
    await presence.connect(b, ws_b, "u1")

    # A's last LOCAL socket goes, but B still holds one: not offline.
    assert await presence.disconnect(a, ws_a) is None
    assert await presence.is_online(a, "u1") is True
    # B's goes too: now the user is gone everywhere.
    assert await presence.disconnect(b, ws_b) == "u1"
    assert await presence.is_online(a, "u1") is False


async def test_disconnect_on_a_only_reports_last(redis, prefix):
    a = _proc(redis, prefix)
    ws = AsyncMock()
    await presence.connect(a, ws, "u1")

    assert await presence.disconnect(a, ws) == "u1"


async def test_disconnect_removes_member_the_manager_already_pruned(redis, prefix):
    """send_to_user prunes dead sockets straight off the manager; the router's
    later disconnect must still drop the cluster member, or it lingers."""
    a, b = _proc(redis, prefix), _proc(redis, prefix)
    ws = AsyncMock()
    await presence.connect(a, ws, "u1")
    await a.disconnect(ws)  # what send_to_user does for a dead socket

    assert await presence.disconnect(a, ws) == "u1"
    assert await presence.is_online(b, "u1") is False


async def test_grace_timer_skips_offline_when_user_is_on_another_process(
    redis, prefix, monkeypatch
):
    import importlib

    from pocketpaw_ee.cloud.realtime.events import PresenceOffline

    chat_router = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    a, b = _proc(redis, prefix), _proc(redis, prefix)
    await presence.connect(b, AsyncMock(), "u1")
    emitted: list = []

    async def fake_emit(ev):
        emitted.append(ev)

    monkeypatch.setattr(chat_router, "manager", a)
    monkeypatch.setattr(chat_router, "emit", fake_emit)
    monkeypatch.setattr(chat_router, "PRESENCE_GRACE_SECONDS", 0.05)

    await chat_router._schedule_presence_offline("u1")
    await asyncio.sleep(0.2)

    assert not any(isinstance(e, PresenceOffline) for e in emitted)


# --- crashed processes ------------------------------------------------------


async def test_crashed_process_members_expire(redis, prefix):
    a = _proc(redis, prefix, stale_seconds=0.4)
    b = _proc(redis, prefix, stale_seconds=0.4)
    await presence.connect(a, AsyncMock(), "u1")
    assert await presence.is_online(b, "u1") is True

    await asyncio.sleep(0.6)  # A "crashed": no heartbeat

    assert await presence.is_online(b, "u1") is False
    # The stale member is pruned, so B's connect is the first again.
    assert await presence.connect(b, AsyncMock(), "u1") is True


async def test_heartbeat_keeps_a_live_process_online(redis, prefix):
    a = _proc(redis, prefix, stale_seconds=0.4)
    b = _proc(redis, prefix, stale_seconds=0.4)
    await presence.connect(a, AsyncMock(), "u1")

    for _ in range(3):
        await asyncio.sleep(0.25)
        await a.presence_registry.heartbeat_once()

    assert await presence.is_online(b, "u1") is True


async def test_background_heartbeat_task_refreshes(redis, prefix):
    a = _proc(redis, prefix, stale_seconds=0.4, heartbeat_seconds=0.1)
    b = _proc(redis, prefix, stale_seconds=0.4)
    await presence.connect(a, AsyncMock(), "u1")
    await a.presence_registry.start()
    try:
        await asyncio.sleep(0.8)
        assert await presence.is_online(b, "u1") is True
    finally:
        await a.presence_registry.stop()
    # A clean stop drops A's members at once.
    assert await presence.is_online(b, "u1") is False


# --- push dispatch ------------------------------------------------------------


async def test_notify_on_a_reaches_user_on_b_over_ws_not_web_push(redis, prefix, monkeypatch):
    from pocketpaw_ee.cloud.chat import ws as ws_mod
    from pocketpaw_ee.cloud.push import dispatch

    stream = f"{prefix}stream"
    a, b = _proc(redis, prefix), _proc(redis, prefix)
    chan_a = Channel(redis, stream=stream, conn_manager=a, block_ms=100)
    chan_b = Channel(redis, stream=stream, conn_manager=b, block_ms=100)
    a.relay_channel, b.relay_channel = chan_a, chan_b
    await chan_a.start()
    await chan_b.start()
    try:
        sock = AsyncMock()
        await presence.connect(b, sock, "u1")
        monkeypatch.setattr(ws_mod, "manager", a)  # dispatch runs on A
        push = AsyncMock()
        monkeypatch.setattr(dispatch.push_service, "send_to_user", push)

        result = await dispatch.notify("w1", "u1", {"title": "hi", "body": "there"})

        assert result.transport == "ws" and result.ws_delivered is True
        push.assert_not_awaited()
        deadline = time.monotonic() + 3
        while not sock.send_json.await_args_list and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        frames = [c.args[0] for c in sock.send_json.await_args_list]
        assert [f["type"] for f in frames] == [dispatch.WS_NOTIFICATION_TYPE]
    finally:
        await chan_a.stop()
        await chan_b.stop()


async def test_notify_uses_web_push_when_user_is_nowhere(redis, prefix, monkeypatch):
    from pocketpaw_ee.cloud.chat import ws as ws_mod
    from pocketpaw_ee.cloud.push import dispatch

    a = _proc(redis, prefix)
    monkeypatch.setattr(ws_mod, "manager", a)
    push = AsyncMock(return_value=None)
    monkeypatch.setattr(dispatch.push_service, "send_to_user", push)

    result = await dispatch.notify("w1", "u1", {"title": "hi", "body": "there"})

    assert result.transport == "push"
    push.assert_awaited_once()


# --- inprocess mode and failures ----------------------------------------------


async def test_inprocess_mode_makes_no_redis_calls(monkeypatch):
    def _no_redis():
        raise AssertionError("inprocess presence must not touch Redis")

    monkeypatch.setattr(presence, "get_redis", _no_redis)
    assert presence.active_registry() is None
    cm = ConnectionManager()
    ws1, ws2 = AsyncMock(), AsyncMock()

    assert await presence.connect(cm, ws1, "u1") is True
    assert await presence.connect(cm, ws2, "u1") is False
    assert await presence.is_online(cm, "u1") is True
    assert await presence.is_online_elsewhere(cm, "u1") is False
    assert await presence.disconnect(cm, ws1) is None
    assert await presence.disconnect(cm, ws2) == "u1"
    assert await presence.is_online(cm, "u1") is False


async def test_redis_failure_falls_back_to_this_process(monkeypatch):
    broken = MagicMock()
    broken.eval = AsyncMock(side_effect=ConnectionError("redis down"))
    broken.pipeline.side_effect = ConnectionError("redis down")
    cm = ConnectionManager()
    cm.presence_registry = Registry(broken, prefix="unused:")
    ws = AsyncMock()

    assert await presence.connect(cm, ws, "u1") is True
    assert await presence.is_online(cm, "u1") is True
    assert await presence.is_online_elsewhere(cm, "u1") is False
    assert await presence.disconnect(cm, ws) == "u1"


# --- the WebSocket endpoint wiring ------------------------------------------------


async def _ws_session(monkeypatch, cm, user_id: str, peers: list[str]):
    """Drive websocket_endpoint on process ``cm`` for one authenticated socket
    that disconnects straight away. Returns (socket, emitted events, users the
    offline grace timer was scheduled for)."""
    import importlib

    from fastapi import WebSocketDisconnect

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    class _Lic:
        expired = False

    monkeypatch.setattr(router_mod, "get_license", lambda: _Lic())
    consume = AsyncMock(return_value=user_id)
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.ws_tickets.consume_ws_ticket", consume)
    monkeypatch.setattr(router_mod, "consume_ws_ticket", consume, raising=False)
    monkeypatch.setattr(router_mod, "manager", cm)
    ws_service = MagicMock()
    ws_service.list_peer_ids = AsyncMock(return_value=peers)
    monkeypatch.setattr(router_mod, "workspace_service", ws_service)
    emitted: list = []

    async def fake_emit(ev):
        emitted.append(ev)

    monkeypatch.setattr(router_mod, "emit", fake_emit)
    scheduled: list[str] = []

    async def fake_schedule(uid):
        scheduled.append(uid)

    monkeypatch.setattr(router_mod, "_schedule_presence_offline", fake_schedule)

    ws = AsyncMock()
    ws.cookies = {}
    ws.receive_text = AsyncMock(
        side_effect=['{"type": "auth", "ticket": "t"}', WebSocketDisconnect()]
    )
    await router_mod.websocket_endpoint(ws, token=None)
    return ws, emitted, scheduled


async def test_endpoint_snapshot_includes_peers_on_other_processes(redis, prefix, monkeypatch):
    a, b = _proc(redis, prefix), _proc(redis, prefix)
    await presence.connect(b, AsyncMock(), "peer")

    ws, _emitted, _scheduled = await _ws_session(monkeypatch, a, "u1", ["peer", "away"])

    frames = [c.args[0] for c in ws.send_json.await_args_list]
    assert {"type": "presence.online", "data": {"user_id": "peer"}} in frames
    assert not any(f.get("data", {}).get("user_id") == "away" for f in frames)


async def test_endpoint_announces_only_a_cluster_wide_first_and_last(redis, prefix, monkeypatch):
    from pocketpaw_ee.cloud.realtime.events import PresenceOnline

    a, b = _proc(redis, prefix), _proc(redis, prefix)
    other = AsyncMock()
    await presence.connect(b, other, "u1")  # u1 already online on B

    _ws, emitted, scheduled = await _ws_session(monkeypatch, a, "u1", [])

    assert not any(isinstance(e, PresenceOnline) for e in emitted)
    assert scheduled == []  # B still holds a socket: no offline timer

    await presence.disconnect(b, other)
    _ws, emitted, scheduled = await _ws_session(monkeypatch, a, "u1", [])

    assert [type(e) for e in emitted] == [PresenceOnline]
    assert scheduled == ["u1"]
