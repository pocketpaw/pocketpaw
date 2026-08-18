"""Tests for the WebSocket connection manager.

Updated: 2026-08-18 (fix/ws-fanout-stale-sockets) — added the fan-out
freshness block at the bottom. ``send_to_user`` / ``send_to_room`` now consult
the same ``_is_fresh`` verdict as ``is_online``: a stale ping-capable socket is
skipped (not counted delivered) and closed best-effort instead of being written
to. ``test_outbound_send_does_not_resurrect_a_zombie`` was restructured to
match — it sends to a fresh socket first, then ages it.

Updated: 2026-08-11 (fix/notif-liveness-dispatch) — added the liveness block at
the bottom. ``is_online`` is now capability-gated: a socket that has proved it
pings is held to an INBOUND-traffic deadline, everything else keeps the legacy
"live while registered" verdict. ``send_to_user`` reports how many sockets
accepted the frame.
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
# Liveness (fix/notif-liveness-dispatch, 2026-08-11)
#
# is_online used to mean "a socket object is in the dict", which a half-open
# socket satisfies forever — so dispatch took the WS leg into a dead pipe and
# skipped Web Push. The verdict is now capability-gated:
#   - ping-capable socket  → live iff an INBOUND frame arrived inside the
#     window. Outbound sends do NOT count: a write to a half-open socket
#     succeeds for minutes, so counting them would resurrect zombies forever.
#   - everything else      → legacy "live while registered", no churn.
# ---------------------------------------------------------------------------


def _backdate_inbound(cm, ws, seconds: float) -> None:
    """Age a socket's last-INBOUND stamp by ``seconds``."""
    cm._ws_last_inbound[ws] = time.monotonic() - seconds


async def _stale_ping_capable(cm, user_id: str = "u1"):
    """A registered, ping-capable socket that has gone silent — the zombie."""
    ws = AsyncMock()
    await cm.connect(ws, user_id)
    cm.mark_ping_capable(ws)
    _backdate_inbound(cm, ws, LIVENESS_STALE_SECONDS + 1)
    return ws


# --- ping-capable sockets are held to the deadline -------------------------


async def test_is_online_false_for_stale_ping_capable_socket(cm):
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    cm.mark_ping_capable(ws)
    assert cm.is_online("u1")

    _backdate_inbound(cm, ws, LIVENESS_STALE_SECONDS + 1)

    assert not cm.is_online("u1")


async def test_stale_socket_is_closed_best_effort(cm):
    # The verdict and the cleanup converge: checking liveness closes the zombie,
    # which wakes the router's receive loop into the normal disconnect path.
    ws = await _stale_ping_capable(cm)

    assert not cm.is_online("u1")
    await asyncio.sleep(0)  # let the fire-and-forget close task run

    assert ws.close.await_count == 1
    # The socket is NOT unregistered here — disconnect() owns that, so the
    # presence.offline grace broadcast still fires through the usual path.
    assert ws in cm.get_user_connections("u1")


async def test_repeated_is_online_does_not_spawn_duplicate_closes(cm):
    # is_online runs on every presence snapshot; without the in-flight guard
    # each call would queue another close task for the same socket.
    ws = await _stale_ping_capable(cm)

    for _ in range(5):
        assert not cm.is_online("u1")

    assert len(cm._close_tasks) == 1
    await asyncio.sleep(0)
    assert ws.close.await_count == 1


async def test_close_task_is_strongly_referenced_until_done(cm):
    # A bare create_task result is only weakly held by the loop, so an
    # unreferenced task can be collected mid-close. The manager holds a ref
    # while it runs and drops it afterwards.
    await _stale_ping_capable(cm)

    assert not cm.is_online("u1")
    assert len(cm._close_tasks) == 1  # held while in flight

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cm._close_tasks == set()  # released once done


async def test_inbound_touch_keeps_socket_live(cm):
    # What the router calls on every inbound frame (ping included).
    ws = await _stale_ping_capable(cm)
    assert not cm.is_online("u1")

    cm.touch(ws)

    assert cm.is_online("u1")


async def test_outbound_send_does_not_resurrect_a_zombie(cm):
    # THE busy-workspace bug: send_json to a half-open socket succeeds (the
    # kernel buffers the write), so if outbound counted as proof of life,
    # ordinary workspace fan-out would keep a dead socket "fresh" forever and
    # the notification would keep going into the void. Send while fresh (so
    # the outbound stamp is recorded), then age the inbound stamp: the accepted
    # write must not rescue the verdict.
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    cm.mark_ping_capable(ws)

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))
    assert delivered == 1  # the write "succeeded"...
    assert ws in cm._ws_last_outbound

    _backdate_inbound(cm, ws, LIVENESS_STALE_SECONDS + 1)

    assert not cm.is_online("u1")  # ...and proved nothing.


async def test_outbound_stamp_is_recorded_for_diagnostics(cm):
    # Outbound bookkeeping is kept (useful when debugging a stuck socket) —
    # it just isn't wired into the verdict.
    ws = AsyncMock()
    await cm.connect(ws, "u1")

    await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert ws in cm._ws_last_outbound


# --- non-ping-capable sockets keep legacy semantics -------------------------


async def test_legacy_socket_stays_online_while_registered(cm):
    # Old FE bundles send nothing for long stretches while perfectly alive.
    # Applying staleness to them would churn every quiet client into a
    # close/reconnect loop, so they keep the pre-change verdict.
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    _backdate_inbound(cm, ws, LIVENESS_STALE_SECONDS * 100)

    assert cm.is_online("u1")


async def test_legacy_socket_is_never_closed_by_a_liveness_check(cm):
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    _backdate_inbound(cm, ws, LIVENESS_STALE_SECONDS * 100)

    cm.is_online("u1")
    await asyncio.sleep(0)

    assert ws.close.await_count == 0


async def test_ping_marks_capability_and_is_idempotent(cm):
    ws = AsyncMock()
    await cm.connect(ws, "u1")

    cm.mark_ping_capable(ws)
    cm.mark_ping_capable(ws)

    assert cm._ping_capable == {ws}


def test_mark_ping_capable_ignores_unknown_socket(cm):
    ws = AsyncMock()
    cm.mark_ping_capable(ws)
    assert cm._ping_capable == set()


async def test_capability_is_cleared_on_disconnect(cm):
    # Otherwise a reconnecting socket object could inherit a stale capability.
    ws = AsyncMock()
    await cm.connect(ws, "u1")
    cm.mark_ping_capable(ws)

    await cm.disconnect(ws)

    assert cm._ping_capable == set()
    assert ws not in cm._ws_last_inbound
    assert ws not in cm._ws_last_outbound


# --- multi-device + delivery counting --------------------------------------


async def test_only_live_socket_makes_user_online(cm):
    # Multi-device: one zombie tab, one live desktop → still online.
    zombie = AsyncMock()
    live = AsyncMock()
    await cm.connect(zombie, "u1")
    await cm.connect(live, "u1")
    cm.mark_ping_capable(zombie)
    cm.mark_ping_capable(live)
    _backdate_inbound(cm, zombie, LIVENESS_STALE_SECONDS + 1)

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


async def test_send_to_user_survives_concurrent_disconnect(cm):
    # send_to_user must iterate a COPY of the live set. A concurrent
    # _close_stale -> disconnect mutates it, and "set changed size during
    # iteration" fires at the `for` — outside the per-send try — so it would
    # escape to notify(), look like a WS failure, and skip the push fallback.
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await cm.connect(ws1, "u1")
    await cm.connect(ws2, "u1")

    async def disconnect_sibling(_data):
        # Mutate the set mid-iteration, exactly as a concurrent prune would.
        await cm.disconnect(ws2)

    ws1.send_json.side_effect = disconnect_sibling

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert delivered >= 1  # no RuntimeError escaped


def test_touch_ignores_unknown_socket(cm):
    # A socket already disconnected must not resurrect itself into the registry.
    ws = AsyncMock()
    cm.touch(ws)
    assert ws not in cm._ws_last_inbound


# ---------------------------------------------------------------------------
# Fan-out freshness (fix/ws-fanout-stale-sockets, 2026-08-18)
#
# The liveness verdict above was only consulted by is_online. Fan-out still
# wrote to every registered socket, so a zombie kept receiving kernel-buffered
# frames AND kept counting toward `delivered` — the signal push/dispatch reads
# to choose WS over Web Push. send_to_user / send_to_room now apply the same
# _is_fresh gate: a stale ping-capable socket is skipped, not counted, and
# closed best-effort exactly as is_online does. Legacy sockets are untouched.
# ---------------------------------------------------------------------------


async def test_send_to_user_skips_stale_ping_capable_socket(cm):
    ws = await _stale_ping_capable(cm)

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert delivered == 0
    ws.send_json.assert_not_awaited()
    # Closed best-effort through the same in-flight-guarded path as is_online:
    # one close task, held strongly, and NOT unregistered here — disconnect()
    # owns that so presence.offline still fires through the receive loop.
    assert ws in cm._closing
    assert len(cm._close_tasks) == 1
    assert ws in cm.get_user_connections("u1")
    await asyncio.sleep(0)
    assert ws.close.await_count == 1


async def test_repeated_sends_do_not_spawn_duplicate_closes_for_stale_socket(cm):
    # Busy-workspace fan-out hits the same zombie many times per second.
    ws = await _stale_ping_capable(cm)

    for _ in range(5):
        assert await cm.send_to_user("u1", WsOutbound(type="test", data={})) == 0

    assert len(cm._close_tasks) == 1
    await asyncio.sleep(0)
    assert ws.close.await_count == 1


async def test_send_to_user_fresh_and_legacy_sockets_still_receive(cm):
    # Multi-device: a fresh ping-capable tab, a legacy (never-pinged) tab, and
    # a zombie. Only the zombie is skipped; legacy keeps live-while-registered.
    fresh = AsyncMock()
    legacy = AsyncMock()
    zombie = AsyncMock()
    await cm.connect(fresh, "u1")
    await cm.connect(legacy, "u1")
    await cm.connect(zombie, "u1")
    cm.mark_ping_capable(fresh)
    cm.mark_ping_capable(zombie)
    _backdate_inbound(cm, legacy, LIVENESS_STALE_SECONDS * 100)
    _backdate_inbound(cm, zombie, LIVENESS_STALE_SECONDS + 1)

    delivered = await cm.send_to_user("u1", WsOutbound(type="test", data={}))

    assert delivered == 2
    fresh.send_json.assert_awaited_once()
    legacy.send_json.assert_awaited_once()
    zombie.send_json.assert_not_awaited()
    assert legacy not in cm._closing


async def test_send_to_room_skips_stale_socket(cm):
    fresh = AsyncMock()
    zombie = AsyncMock()
    await cm.connect(fresh, "u1")
    await cm.connect(zombie, "u2")
    cm.join_room(fresh, "g1")
    cm.join_room(zombie, "g1")
    cm.mark_ping_capable(zombie)
    _backdate_inbound(cm, zombie, LIVENESS_STALE_SECONDS + 1)

    await cm.send_to_room("g1", WsOutbound(type="typing", data={}))

    fresh.send_json.assert_awaited_once()
    zombie.send_json.assert_not_awaited()
    assert zombie in cm._closing
    assert zombie in cm.get_user_connections("u2")  # disconnect() owns removal
    await asyncio.sleep(0)
    assert zombie.close.await_count == 1
