"""Tests that the chat router emits realtime events for transient signals.

Covers ``typing.start`` / ``typing.stop`` / ``message.read`` — transient
signals that don't go through ``message_service`` but still need to fire
``emit()`` so other backends + the cross-process bus stay in sync.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud.chat.schemas import WsInbound
from pocketpaw_ee.cloud.realtime.events import MessageRead, TypingStart, TypingStop


@pytest.mark.asyncio
async def test_ws_typing_start_emits_typing_start(monkeypatch, recording_bus):
    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", AsyncMock())
    monkeypatch.setattr(router_mod.manager, "start_typing", MagicMock())

    await router_mod._ws_typing(
        user_id="u1",
        msg=WsInbound(type="typing.start", group_id="g1"),
        active=True,
    )

    starts = [e for e in recording_bus.events if isinstance(e, TypingStart)]
    assert len(starts) == 1
    assert starts[0].data["group_id"] == "g1"
    assert starts[0].data["user_id"] == "u1"


@pytest.mark.asyncio
async def test_ws_typing_stop_emits_typing_stop(monkeypatch, recording_bus):
    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", AsyncMock())
    monkeypatch.setattr(router_mod.manager, "stop_typing", MagicMock())

    await router_mod._ws_typing(
        user_id="u1",
        msg=WsInbound(type="typing.stop", group_id="g1"),
        active=False,
    )

    stops = [e for e in recording_bus.events if isinstance(e, TypingStop)]
    assert len(stops) == 1
    assert stops[0].data["group_id"] == "g1"
    assert stops[0].data["user_id"] == "u1"


@pytest.mark.asyncio
async def test_ws_typing_non_member_does_not_emit(monkeypatch, recording_bus):
    """Membership check must still gate the emit — no spoofing."""
    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", AsyncMock())
    monkeypatch.setattr(router_mod.manager, "start_typing", MagicMock())

    await router_mod._ws_typing(
        user_id="intruder",
        msg=WsInbound(type="typing.start", group_id="g1"),
        active=True,
    )

    assert not [e for e in recording_bus.events if isinstance(e, TypingStart | TypingStop)]


@pytest.mark.asyncio
async def test_ws_read_ack_emits_message_read(monkeypatch, recording_bus):
    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    async def fake_mark_read(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.unread_service, "mark_read", fake_mark_read)
    monkeypatch.setattr(router_mod.manager, "send_to_room", AsyncMock())

    await router_mod._ws_read_ack(
        user_id="u1",
        msg=WsInbound(type="read.ack", group_id="g1", message_id="m1"),
    )

    reads = [e for e in recording_bus.events if isinstance(e, MessageRead)]
    assert len(reads) == 1
    assert reads[0].data["group_id"] == "g1"
    assert reads[0].data["user_id"] == "u1"
    assert reads[0].data["message_id"] == "m1"


@pytest.mark.asyncio
async def test_ws_read_ack_non_member_does_not_emit(monkeypatch, recording_bus):
    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    monkeypatch.setattr(router_mod.group_service, "list_member_ids", members)
    monkeypatch.setattr(router_mod.manager, "send_to_room", AsyncMock())

    await router_mod._ws_read_ack(
        user_id="intruder",
        msg=WsInbound(type="read.ack", group_id="g1", message_id="m1"),
    )

    assert not [e for e in recording_bus.events if isinstance(e, MessageRead)]
