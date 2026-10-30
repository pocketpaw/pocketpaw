# test_bus_subscribe.py — tests for InProcessBus.subscribe (Stage 1.B).
# Created: 2026-04-30 — Stage 1.B "Files as Knowledge". Verifies the new
#   in-process subscriber API: subscribe + publish round trip, multiple
#   handlers, exception isolation, no handler is a no-op.
"""Tests for ``InProcessBus`` in-process subscriber support."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud._core.realtime.events import FileReady, MessageSent


@pytest.mark.asyncio
async def test_subscribe_then_publish_invokes_handler():
    bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())

    received: list = []

    async def handler(ev):
        received.append(ev)

    bus.subscribe(FileReady.EVENT_TYPE, handler)

    ev = FileReady(data={"workspace_id": "w1", "file_id": "f1"})
    await bus.publish(ev)

    assert received == [ev]


@pytest.mark.asyncio
async def test_subscribe_multiple_handlers_all_called():
    bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())

    calls: list[str] = []

    async def first(ev):  # noqa: ARG001
        calls.append("first")

    async def second(ev):  # noqa: ARG001
        calls.append("second")

    bus.subscribe(FileReady.EVENT_TYPE, first)
    bus.subscribe(FileReady.EVENT_TYPE, second)

    await bus.publish(FileReady(data={"workspace_id": "w1", "file_id": "f1"}))

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_handler_exception_does_not_block_other_handlers(caplog):
    bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())

    seen: list[str] = []

    async def broken(ev):  # noqa: ARG001
        raise RuntimeError("boom")

    async def healthy(ev):  # noqa: ARG001
        seen.append("ok")

    bus.subscribe(FileReady.EVENT_TYPE, broken)
    bus.subscribe(FileReady.EVENT_TYPE, healthy)

    with caplog.at_level("ERROR"):
        await bus.publish(FileReady(data={"workspace_id": "w1", "file_id": "f1"}))

    assert seen == ["ok"]
    assert any("local handler failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_websocket_fanout():
    """WebSocket audience must still receive the event even if a local
    handler explodes. The broadcast runs first, so this is mainly a guard
    against future re-orderings."""
    conn = AsyncMock()
    bus = InProcessBus(resolver=AudienceResolver(), conn_manager=conn)

    async def broken(ev):  # noqa: ARG001
        raise RuntimeError("boom")

    bus.subscribe(MessageSent.EVENT_TYPE, broken)

    # MessageSent's audience is [data["sender_id"]], so we get a deterministic
    # send_to_user invocation we can inspect.
    await bus.publish(MessageSent(data={"sender_id": "u1", "group_id": "g"}))

    conn.send_to_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_with_no_handlers_is_a_noop():
    bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())

    # No subscribers registered — publish must not raise even when nothing
    # listens for the event.
    await bus.publish(FileReady(data={"workspace_id": "w1", "file_id": "f1"}))


@pytest.mark.asyncio
async def test_subscribe_filters_by_event_type():
    """A subscriber on ``file.ready`` must not see ``message.sent``."""
    bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())

    received: list = []

    async def handler(ev):
        received.append(ev)

    bus.subscribe(FileReady.EVENT_TYPE, handler)

    await bus.publish(MessageSent(data={"sender_id": "u1", "group_id": "g"}))
    assert received == []

    file_ev = FileReady(data={"workspace_id": "w1", "file_id": "f1"})
    await bus.publish(file_ev)
    assert received == [file_ev]
