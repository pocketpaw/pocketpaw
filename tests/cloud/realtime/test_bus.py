"""Tests for InProcessBus."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus, get_bus, set_bus
from pocketpaw_ee.cloud._core.realtime.events import GroupCreated, MessageSent


@pytest.mark.asyncio
async def test_inprocess_bus_fans_out_to_resolved_audience():
    resolver = AudienceResolver()
    conn = AsyncMock()
    bus = InProcessBus(resolver=resolver, conn_manager=conn)
    ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1", "u2"]})

    await bus.publish(ev)

    assert conn.send_to_user.await_count == 2
    sent = {call.args[0] for call in conn.send_to_user.await_args_list}
    assert sent == {"u1", "u2"}


@pytest.mark.asyncio
async def test_inprocess_bus_sends_correct_payload():
    resolver = AudienceResolver()
    conn = AsyncMock()
    bus = InProcessBus(resolver=resolver, conn_manager=conn)
    ev = MessageSent(data={"group_id": "g", "sender_id": "u1"})

    await bus.publish(ev)

    conn.send_to_user.assert_awaited_once()
    user_arg, payload = conn.send_to_user.await_args.args
    assert user_arg == "u1"
    # payload is a WsOutbound with type + data
    assert payload.type == "message.sent"
    assert payload.data == {"group_id": "g", "sender_id": "u1"}


@pytest.mark.asyncio
async def test_inprocess_bus_isolates_per_recipient_exceptions():
    resolver = AudienceResolver()
    conn = AsyncMock()
    # Middle recipient fails; third should still receive
    conn.send_to_user.side_effect = [None, RuntimeError("dead socket"), None]
    bus = InProcessBus(resolver=resolver, conn_manager=conn)
    ev = GroupCreated(data={"group_id": "g", "member_ids": ["u1", "u2", "u3"]})

    await bus.publish(ev)

    assert conn.send_to_user.await_count == 3


@pytest.mark.asyncio
async def test_inprocess_bus_swallows_audience_resolution_errors():
    # Force resolver to raise
    class BrokenResolver:
        async def audience(self, _ev):
            raise RuntimeError("db exploded")

    conn = AsyncMock()
    bus = InProcessBus(resolver=BrokenResolver(), conn_manager=conn)
    # Must not raise — emit should never break the caller's mutation
    await bus.publish(GroupCreated(data={"group_id": "g", "member_ids": ["u1"]}))
    conn.send_to_user.assert_not_called()


def test_module_singleton_get_raises_if_not_set():
    # Reset singleton state
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    bus_mod._bus = None  # type: ignore[attr-defined]
    with pytest.raises(AssertionError):
        get_bus()


def test_module_singleton_set_then_get():
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    bus_mod._bus = None  # type: ignore[attr-defined]
    dummy = object()
    set_bus(dummy)  # type: ignore[arg-type]
    assert get_bus() is dummy
