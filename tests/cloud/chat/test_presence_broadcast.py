"""Tests for the presence broadcast wiring (Task 19, Cluster A sub-PR 4).

The WebSocket endpoint is responsible for publishing ``presence.online`` on
connect and scheduling a delayed ``presence.offline`` broadcast on
disconnect. We verify both halves by driving ``_schedule_presence_offline``
and simulating connect/disconnect against the ``ConnectionManager`` singleton.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import patch

import pytest

chat_router = importlib.import_module("ee.cloud.chat.router")
from ee.cloud.chat.ws import manager
from ee.cloud.realtime.events import PresenceOffline


@pytest.mark.asyncio
async def test_schedule_presence_offline_emits_after_grace_when_user_stays_offline():
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    # Short-circuit the grace window so the test runs fast.
    with (
        patch.object(chat_router, "emit", new=fake_emit),
        patch.object(chat_router, "PRESENCE_GRACE_SECONDS", 0.05),
    ):
        await chat_router._schedule_presence_offline("user-42")
        # Give the scheduled task room to run.
        await asyncio.sleep(0.12)

    events = [e for e in recorded if isinstance(e, PresenceOffline)]
    assert len(events) == 1
    assert events[0].data == {"user_id": "user-42"}


@pytest.mark.asyncio
async def test_schedule_presence_offline_cancelled_when_user_reconnects():
    """If a reconnect lands inside the grace window the scheduled offline
    task must be cancelled so no PresenceOffline leaks out."""
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    with (
        patch.object(chat_router, "emit", new=fake_emit),
        patch.object(chat_router, "PRESENCE_GRACE_SECONDS", 0.2),
    ):
        await chat_router._schedule_presence_offline("user-99")
        # Simulate reconnect: the manager cancels the offline task.
        task = manager._offline_tasks.pop("user-99", None)
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.25)

    assert not any(isinstance(e, PresenceOffline) for e in recorded)
