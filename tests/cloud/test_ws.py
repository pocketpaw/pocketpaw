"""Tests for the WebSocket connection manager.

Updated: 2026-08-11 (fix/notif-liveness-dispatch) — added the liveness block at
the bottom: ``is_online`` now means "traffic seen inside the window", and
``send_to_user`` reports how many sockets accepted the frame.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.chat.schemas import WsOutbound
from pocketpaw_ee.cloud.chat.ws import LIVENESS_STALE_SECONDS, ConnectionManager


@pytest.fixture
def cm():
    return ConnectionManager()


def test_init():
    cm = ConnectionManager()
    assert cm.active_connections == {}


def test_get_user_connections_empty(cm):
    assert cm.get_user_connections("u1") == set()


def test_is_online_false(cm):
    assert not cm.is_online("u1")


async def test_connect(cm):
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    assert cm.is_online("u1")
    assert ws in cm.get_user_connections("u1")


async def test_multi_device(cm):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await cm.connect(ws1, "u1")
    await cm.connect(ws2, "u1")
    assert len(cm.get_user_connections("u1")) == 2


async def test_disconnect_returns_user_on_last(cm):
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    user_id = await cm.disconnect(ws)
    assert user_id == "u1"
    assert not cm.is_online("u1")


async def test_disconnect_returns_none_if_more(cm):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await cm.connect(ws1, "u1")
    await cm.connect(ws2, "u1")
    user_id = await cm.disconnect(ws1)
    assert user_id is None  # Still has ws2
    assert cm.is_online("u1")


async def test_send_to_user(cm):
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    msg = WsOutbound(type="test", data={"hello": "world"})
    await cm.send_to_user("u1", msg)
    ws.send_json.assert_called_once()


async def test_send_to_user_multi_device(cm):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await cm.connect(ws1, "u1")
    await cm.connect(ws2, "u1")
    msg = WsOutbound(type="test", data={"x": 1})
    await cm.send_to_user("u1", msg)
    ws1.send_json.assert_called_once()
    ws2.send_json.assert_called_once()


async def test_send_to_user_no_connections(cm):
    """Sending to a user with no connections should not raise."""
    msg = WsOutbound(type="test", data={})
    await cm.send_to_user("nobody", msg)  # should be a no-op


async def test_send_to_user_dead_connection_cleaned(cm):
    ws_good = AsyncMock()
    ws_dead = AsyncMock()
    ws_dead.send_json.side_effect = RuntimeError("connection closed")
    await cm.connect(ws_good, "u1")
    await cm.connect(ws_dead, "u1")
    msg = WsOutbound(type="test", data={})
    await cm.send_to_user("u1", msg)
    # Dead connection should be removed
    assert ws_dead not in cm.get_user_connections("u1")
    assert ws_good in cm.get_user_connections("u1")


async def test_broadcast_to_group(cm):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    ws3 = AsyncMock()
    await cm.connect(ws1, "u1")
    await cm.connect(ws2, "u2")
    await cm.connect(ws3, "u3")
    msg = WsOutbound(type="message.new", data={})
    await cm.broadcast_to_group("g1", ["u1", "u2", "u3"], msg, exclude_user="u1")
    ws1.send_json.assert_not_called()  # excluded
    ws2.send_json.assert_called_once()
    ws3.send_json.assert_called_once()


async def test_broadcast_to_group_no_exclude(cm):
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await cm.connect(ws1, "u1")
    await cm.connect(ws2, "u2")
    msg = WsOutbound(type="message.new", data={})
    await cm.broadcast_to_group("g1", ["u1", "u2"], msg)
    ws1.send_json.assert_called_once()
    ws2.send_json.assert_called_once()


async def test_disconnect_unknown_ws(cm):
    ws = AsyncMock()
    result = await cm.disconnect(ws)
    assert result is None


async def test_typing_tracking(cm):
    cm.start_typing("g1", "u1")
    assert cm.is_typing("g1", "u1")
    cm.stop_typing("g1", "u1")
    assert not cm.is_typing("g1", "u1")


async def test_typing_stop_idempotent(cm):
    """Stopping typing when not typing should not raise."""
    cm.stop_typing("g1", "u1")  # no-op


async def test_typing_restart_resets_timer(cm):
    """Starting typing twice should cancel the first timer."""
    cm.start_typing("g1", "u1")
    cm.start_typing("g1", "u1")  # should replace, not stack
    assert cm.is_typing("g1", "u1")
    cm.stop_typing("g1", "u1")
    assert not cm.is_typing("g1", "u1")


async def test_typing_auto_expires(cm):
    """Typing indicator should auto-expire after timeout."""
    cm.start_typing("g1", "u1")
    assert cm.is_typing("g1", "u1")
    # Wait for the typing timeout (5s) — use a shorter sleep to be safe
    await asyncio.sleep(6)
    assert not cm.is_typing("g1", "u1")


async def test_connect_cancels_pending_offline_task(cm):
    """Reconnecting should cancel any pending offline grace period task."""
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await cm.connect(ws1, "u1")

    # Simulate disconnect triggering offline task
    user_id = await cm.disconnect(ws1)
    assert user_id == "u1"

    # Create a fake offline task
    task = asyncio.create_task(asyncio.sleep(30))
    cm._offline_tasks["u1"] = task

    # Reconnect should cancel the offline task
    await cm.connect(ws2, "u1")
    # Yield control so the cancellation propagates
    await asyncio.sleep(0)
    assert task.cancelled()
    assert "u1" not in cm._offline_tasks


# ---------------------------------------------------------------------------
# Liveness — is_online means "traffic seen recently", not "socket in the dict"
# (fix/notif-liveness-dispatch, 2026-08-11). A half-open socket used to report
# online forever, so notification dispatch took the WS leg into a dead pipe and
# skipped Web Push. These pin the traffic-based verdict in both directions.
# ---------------------------------------------------------------------------


def _backdate(cm, ws, seconds: float) -> None:
    """Age a socket's last-activity stamp by ``seconds``."""
    cm._ws_last_seen[ws] = time.monotonic() - seconds


async def test_is_online_false_for_stale_socket(cm):
    # The zombie: registered, never closed, silent past the liveness window.
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    assert cm.is_online("u1")

    _backdate(cm, ws, LIVENESS_STALE_SECONDS + 1)

    assert not cm.is_online("u1")


async def test_stale_socket_is_closed_best_effort(cm):
    # The verdict and the cleanup converge: checking liveness closes the zombie,
    # which wakes the router's receive loop into the normal disconnect path.
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    _backdate(cm, ws, LIVENESS_STALE_SECONDS + 1)

    assert not cm.is_online("u1")
    await asyncio.sleep(0)  # let the fire-and-forget close task run

    assert ws.close.await_count == 1
    # The socket is NOT unregistered here — disconnect() owns that, so the
    # presence.offline grace broadcast still fires through the usual path.
    assert ws in cm.get_user_connections("u1")


async def test_inbound_touch_keeps_socket_live(cm):
    # What the router calls on every inbound frame (ping included).
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    _backdate(cm, ws, LIVENESS_STALE_SECONDS + 1)
    assert not cm.is_online("u1")

    cm.touch(ws)

    assert cm.is_online("u1")


async def test_successful_send_keeps_idle_socket_live(cm):
    # Backward-compat clause: today's clients send nothing for long stretches
    # while idle-but-alive, so an outbound send the socket ACCEPTED counts as
    # proof of life. Without this, every quiet client would look like a zombie.
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    _backdate(cm, ws, LIVENESS_STALE_SECONDS + 1)
    assert not cm.is_online("u1")

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert delivered == 1
    assert cm.is_online("u1")


async def test_only_live_socket_makes_user_online(cm):
    # Multi-device: one zombie tab, one live desktop → still online.
    zombie = AsyncMock()
    live = AsyncMock()
    await cm.connect(zombie, "u1")
    await cm.connect(live, "u1")
    _backdate(cm, zombie, LIVENESS_STALE_SECONDS + 1)

    assert cm.is_online("u1")


async def test_send_to_user_returns_delivery_count(cm):
    # The signal dispatch.notify reads: zero delivered means "looked live,
    # reached nobody" and must fall back to Web Push.
    dead = AsyncMock()
    dead.send_json.side_effect = RuntimeError("socket closed")
    await cm.connect(dead, "u1")

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert delivered == 0
    # The dead socket was pruned by the send itself.
    assert cm.get_user_connections("u1") == set()


async def test_send_to_user_counts_only_accepted_sockets(cm):
    ok = AsyncMock()
    dead = AsyncMock()
    dead.send_json.side_effect = RuntimeError("socket closed")
    await cm.connect(ok, "u1")
    await cm.connect(dead, "u1")

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert delivered == 1


def test_touch_ignores_unknown_socket(cm):
    # A socket already disconnected must not resurrect itself into the registry.
    ws = AsyncMock()
    cm.touch(ws)
    assert ws not in cm._ws_last_seen
