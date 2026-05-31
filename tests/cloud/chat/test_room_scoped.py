"""Room-scoped routing for typing + read receipts."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_join_room_tracks_single_current_room():
    from pocketpaw_ee.cloud.chat.ws import ConnectionManager

    mgr = ConnectionManager()
    ws = AsyncMock()
    ws.send_json = AsyncMock()

    await mgr.connect(ws, "u1")
    mgr.join_room(ws, "g1")

    assert mgr.current_room(ws) == "g1"

    # Joining a second room replaces — one room per socket
    mgr.join_room(ws, "g2")
    assert mgr.current_room(ws) == "g2"


@pytest.mark.asyncio
async def test_leave_room_clears_current_room():
    from pocketpaw_ee.cloud.chat.ws import ConnectionManager

    mgr = ConnectionManager()
    ws = AsyncMock()
    ws.send_json = AsyncMock()

    await mgr.connect(ws, "u1")
    mgr.join_room(ws, "g1")
    mgr.leave_room(ws)

    assert mgr.current_room(ws) is None


@pytest.mark.asyncio
async def test_send_to_room_only_delivers_to_joined_sockets():
    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import ConnectionManager

    mgr = ConnectionManager()

    ws_in_room = AsyncMock()
    ws_in_room.send_json = AsyncMock()
    ws_other_room = AsyncMock()
    ws_other_room.send_json = AsyncMock()
    ws_no_room = AsyncMock()
    ws_no_room.send_json = AsyncMock()

    await mgr.connect(ws_in_room, "u1")
    await mgr.connect(ws_other_room, "u2")
    await mgr.connect(ws_no_room, "u3")

    mgr.join_room(ws_in_room, "g1")
    mgr.join_room(ws_other_room, "g99")

    payload = WsOutbound(type="typing", data={"group_id": "g1", "user_id": "ux", "active": True})
    await mgr.send_to_room("g1", payload)

    ws_in_room.send_json.assert_awaited_once()
    ws_other_room.send_json.assert_not_called()
    ws_no_room.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_send_to_room_excludes_user():
    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import ConnectionManager

    mgr = ConnectionManager()
    ws_sender = AsyncMock()
    ws_sender.send_json = AsyncMock()
    ws_peer = AsyncMock()
    ws_peer.send_json = AsyncMock()

    await mgr.connect(ws_sender, "u1")
    await mgr.connect(ws_peer, "u2")
    mgr.join_room(ws_sender, "g1")
    mgr.join_room(ws_peer, "g1")

    payload = WsOutbound(type="typing", data={"group_id": "g1", "user_id": "u1", "active": True})
    await mgr.send_to_room("g1", payload, exclude_user="u1")

    ws_sender.send_json.assert_not_called()
    ws_peer.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_clears_current_room():
    from pocketpaw_ee.cloud.chat.ws import ConnectionManager

    mgr = ConnectionManager()
    ws = AsyncMock()
    ws.send_json = AsyncMock()

    await mgr.connect(ws, "u1")
    mgr.join_room(ws, "g1")
    await mgr.disconnect(ws)

    assert mgr.current_room(ws) is None


@pytest.mark.asyncio
async def test_room_join_rejects_non_member(monkeypatch):
    """An authenticated user cannot join a group they are not a member of."""
    import importlib
    from unittest.mock import AsyncMock as _AsyncMock

    from pocketpaw_ee.cloud.chat.schemas import WsInbound

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["someone-else", "another-user"]

    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "join_room", _AsyncMock())

    ws = _AsyncMock()
    await router_mod._handle_ws_message(
        ws, user_id="intruder", msg=WsInbound(type="room.join", group_id="g1")
    )

    router_mod.manager.join_room.assert_not_called()


@pytest.mark.asyncio
async def test_room_join_allows_member(monkeypatch):
    """A group member can join their own group."""
    import importlib
    from unittest.mock import MagicMock

    from pocketpaw_ee.cloud.chat.schemas import WsInbound

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    join_spy = MagicMock()
    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "join_room", join_spy)

    ws = MagicMock()
    await router_mod._handle_ws_message(
        ws, user_id="u1", msg=WsInbound(type="room.join", group_id="g1")
    )

    join_spy.assert_called_once_with(ws, "g1")


# ---------------------------------------------------------------------------
# Typing / read-ack membership enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_rejects_non_member(monkeypatch):
    """A non-member cannot spoof typing indicators into a group."""
    import importlib
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock

    from pocketpaw_ee.cloud.chat.schemas import WsInbound

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    send_spy = _AsyncMock()
    start_typing_spy = MagicMock()
    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", send_spy)
    monkeypatch.setattr(router_mod.manager, "start_typing", start_typing_spy)

    await router_mod._ws_typing(
        user_id="intruder",
        msg=WsInbound(type="typing.start", group_id="g1"),
        active=True,
    )

    send_spy.assert_not_called()
    start_typing_spy.assert_not_called()


@pytest.mark.asyncio
async def test_typing_allows_member(monkeypatch):
    """A real member's typing event is broadcast to the room."""
    import importlib
    from unittest.mock import AsyncMock as _AsyncMock
    from unittest.mock import MagicMock

    from pocketpaw_ee.cloud.chat.schemas import WsInbound

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    send_spy = _AsyncMock()
    start_typing_spy = MagicMock()
    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", send_spy)
    monkeypatch.setattr(router_mod.manager, "start_typing", start_typing_spy)

    await router_mod._ws_typing(
        user_id="u1",
        msg=WsInbound(type="typing.start", group_id="g1"),
        active=True,
    )

    start_typing_spy.assert_called_once_with("g1", "u1")
    send_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_ack_rejects_non_member(monkeypatch):
    """A non-member cannot spoof a read receipt for a group they aren't in."""
    import importlib
    from unittest.mock import AsyncMock as _AsyncMock

    from pocketpaw_ee.cloud.chat.schemas import WsInbound

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    send_spy = _AsyncMock()
    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", send_spy)

    await router_mod._ws_read_ack(
        user_id="intruder",
        msg=WsInbound(type="read.ack", group_id="g1", message_id="m1"),
    )

    send_spy.assert_not_called()


@pytest.mark.asyncio
async def test_read_ack_allows_member(monkeypatch):
    """A real member's read receipt is broadcast to the room."""
    import importlib
    from unittest.mock import AsyncMock as _AsyncMock

    from pocketpaw_ee.cloud.chat.schemas import WsInbound

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    send_spy = _AsyncMock()
    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", send_spy)

    await router_mod._ws_read_ack(
        user_id="u1",
        msg=WsInbound(type="read.ack", group_id="g1", message_id="m1"),
    )

    send_spy.assert_awaited_once()
