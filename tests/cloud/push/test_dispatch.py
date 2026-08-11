# Tests for the notification dispatch + WS-vs-Web-Push dedupe (pocketpaw#1393).
# Created: 2026-06-09 (feat/push-wire-events) — exercises ``dispatch.notify``:
#   - a LIVE user is delivered over WebSocket and Web Push is NEVER sent
#     (the dedupe — verified by asserting send_to_user was not called);
#   - an OFFLINE (browser-only / backgrounded) user falls back to Web Push;
#   - a desktop-only live user takes the WS/native path;
#   - the returned NotifyResult records the transport for observability;
#   - a WS delivery failure is reported (not silently double-sent over push).
# The WS layer and the push send path are both mocked — no real sockets, no
# network. The liveness check and the WS send are patched via the module's
# injectable seams (``_is_user_live`` / ``_send_over_ws``); one integration
# test drives the real ConnectionManager singleton to prove the seam matches.
# Updated: 2026-08-11 (fix/notif-liveness-dispatch) — ``_send_over_ws`` now
# returns the number of sockets that actually accepted the frame, so the
# zombie case (user "has" a socket, delivery reaches ZERO of them) falls back
# to Web Push instead of dropping the notification. The seam fakes therefore
# return a count, and a new test pins ``transport == "ws_fallback_push"``.

from __future__ import annotations

from pocketpaw_ee.cloud.push import dispatch
from pocketpaw_ee.cloud.push.dto import PushPayload, SendResult

_PAYLOAD = {"title": "Hi", "body": "There", "url": "/inbox"}


# ---------------------------------------------------------------------------
# Dedupe — live user gets WS only, offline user gets Web Push only.
# ---------------------------------------------------------------------------


async def test_live_user_delivers_ws_and_skips_web_push(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "_is_user_live", lambda uid: True)

    ws_sent: list[tuple[str, PushPayload]] = []

    async def fake_ws(user_id: str, payload: PushPayload) -> int:
        ws_sent.append((user_id, payload))
        return 1  # one socket accepted the frame

    monkeypatch.setattr(dispatch, "_send_over_ws", fake_ws)

    # Web Push must NOT be touched on the live path.
    push_calls: list = []

    async def fake_send(workspace_id, user_id, payload):
        push_calls.append((workspace_id, user_id))
        return SendResult(sent=1)

    monkeypatch.setattr(dispatch.push_service, "send_to_user", fake_send)

    result = await dispatch.notify("w1", "u1", _PAYLOAD)

    assert result.transport == "ws"
    assert result.ws_delivered is True
    assert result.send is None
    # Delivered over WS exactly once...
    assert len(ws_sent) == 1
    assert ws_sent[0][0] == "u1"
    assert ws_sent[0][1].title == "Hi"
    # ...and Web Push was never sent (the dedupe).
    assert push_calls == []


async def test_offline_user_falls_back_to_web_push(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "_is_user_live", lambda uid: False)

    ws_calls: list = []

    async def fake_ws(user_id, payload):
        ws_calls.append(user_id)

    monkeypatch.setattr(dispatch, "_send_over_ws", fake_ws)

    push_calls: list = []

    async def fake_send(workspace_id, user_id, payload):
        push_calls.append((workspace_id, user_id, payload))
        return SendResult(sent=2, pruned=1)

    monkeypatch.setattr(dispatch.push_service, "send_to_user", fake_send)

    result = await dispatch.notify("w1", "u2", _PAYLOAD)

    assert result.transport == "push"
    assert result.ws_delivered is False
    assert result.send is not None
    assert result.send.sent == 2
    assert result.send.pruned == 1
    # Web Push got the call...
    assert len(push_calls) == 1
    assert push_calls[0][0] == "w1"
    assert push_calls[0][1] == "u2"
    # ...and WS was never used on the offline path.
    assert ws_calls == []


async def test_dispatch_validates_dict_payload(monkeypatch) -> None:
    # A raw dict from a bus listener is validated into a PushPayload at entry.
    monkeypatch.setattr(dispatch, "_is_user_live", lambda uid: True)
    captured: dict = {}

    async def fake_ws(user_id, payload):
        captured["payload"] = payload
        return 1

    monkeypatch.setattr(dispatch, "_send_over_ws", fake_ws)

    await dispatch.notify("w1", "u1", {"title": "T", "body": "B"})

    assert isinstance(captured["payload"], PushPayload)
    assert captured["payload"].title == "T"


async def test_ws_failure_does_not_double_send_over_push(monkeypatch) -> None:
    # If the WS leg raises, we report a failed WS delivery rather than also
    # firing Web Push (no double-notify).
    monkeypatch.setattr(dispatch, "_is_user_live", lambda uid: True)

    async def boom(user_id, payload):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(dispatch, "_send_over_ws", boom)

    push_calls: list = []

    async def fake_send(workspace_id, user_id, payload):
        push_calls.append(user_id)
        return SendResult()

    monkeypatch.setattr(dispatch.push_service, "send_to_user", fake_send)

    result = await dispatch.notify("w1", "u1", _PAYLOAD)

    assert result.transport == "ws"
    assert result.ws_delivered is False
    assert push_calls == []  # Web Push was NOT used as a fallback.


# ---------------------------------------------------------------------------
# Zombie socket — "live" by the registry, but the frame reaches nobody.
# ---------------------------------------------------------------------------


async def test_zombie_socket_falls_back_to_web_push(monkeypatch) -> None:
    # The half-open socket case: the connection registry still holds a socket
    # for the user (laptop asleep, NAT timeout — the TCP close never arrived),
    # so the liveness check says live, but the send reaches ZERO sockets. Before
    # the fix the notification was dropped on the floor: WS leg taken, Web Push
    # skipped by the dedupe, nothing delivered. Now the zero-delivery signal
    # falls the dispatch through to Web Push in the same call.
    monkeypatch.setattr(dispatch, "_is_user_live", lambda uid: True)

    async def dead_ws(user_id, payload):
        return 0  # every socket was dead and got pruned mid-send

    monkeypatch.setattr(dispatch, "_send_over_ws", dead_ws)

    push_calls: list = []

    async def fake_send(workspace_id, user_id, payload):
        push_calls.append((workspace_id, user_id, payload))
        return SendResult(sent=1, pruned=1)

    monkeypatch.setattr(dispatch.push_service, "send_to_user", fake_send)

    result = await dispatch.notify("w1", "zombie", _PAYLOAD)

    assert result.transport == "ws_fallback_push"
    assert result.ws_delivered is False
    # The Web Push leg actually ran, with the same payload.
    assert len(push_calls) == 1
    assert push_calls[0][1] == "zombie"
    assert push_calls[0][2].title == "Hi"
    assert result.send is not None
    assert result.send.sent == 1


# ---------------------------------------------------------------------------
# Integration — the real ConnectionManager singleton drives the liveness seam.
# ---------------------------------------------------------------------------


async def test_liveness_seam_reflects_connection_manager(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from pocketpaw_ee.cloud.chat.ws import manager

    # Web Push is mocked so the offline branch never hits the network.
    async def fake_send(workspace_id, user_id, payload):
        return SendResult()

    monkeypatch.setattr(dispatch.push_service, "send_to_user", fake_send)

    # Browser-only user (no connection) → Web Push.
    offline = await dispatch.notify("w1", "ghost", _PAYLOAD)
    assert offline.transport == "push"

    # Connect a fake socket for the user on the real singleton, then dispatch:
    # the same manager the bus uses now reports the user live → WS path.
    ws = AsyncMock()
    await manager.connect(ws, "desktop-user")
    try:
        live = await dispatch.notify("w1", "desktop-user", _PAYLOAD)
        assert live.transport == "ws"
        assert live.ws_delivered is True
        # The WS message was actually pushed to the fake socket.
        assert ws.send_json.await_count == 1
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == dispatch.WS_NOTIFICATION_TYPE
        assert sent["data"]["title"] == "Hi"
    finally:
        await manager.disconnect(ws)
