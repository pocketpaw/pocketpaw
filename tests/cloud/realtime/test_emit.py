"""Tests for the emit() facade."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
from pocketpaw_ee.cloud._core.realtime.bus import set_bus
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import GroupCreated


@pytest.mark.asyncio
async def test_emit_delegates_to_active_bus():
    stub_bus = AsyncMock()
    set_bus(stub_bus)
    ev = GroupCreated(data={"group_id": "g", "member_ids": ["u1"]})

    await emit(ev)

    stub_bus.publish.assert_awaited_once_with(ev)


@pytest.mark.asyncio
async def test_emit_swallows_bus_errors():
    class BrokenBus:
        async def publish(self, _ev):
            raise RuntimeError("redis offline")

    set_bus(BrokenBus())  # type: ignore[arg-type]
    # Caller's mutation must not be aborted by emit failure
    await emit(GroupCreated(data={"group_id": "g", "member_ids": ["u1"]}))


@pytest.mark.asyncio
async def test_emit_raises_if_bus_not_initialized():
    bus_mod._bus = None  # type: ignore[attr-defined]
    with pytest.raises(AssertionError):
        await emit(GroupCreated(data={"group_id": "g", "member_ids": []}))
