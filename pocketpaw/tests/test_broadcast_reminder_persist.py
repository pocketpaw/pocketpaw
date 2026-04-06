"""Tests for broadcast_reminder() session-history persistence.

Verifies that when a reminder fires, the reminder message is saved to the
session history of every active WebSocket connection so it survives tab
switches and page reloads (fixes GitHub issue #364).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestBroadcastReminderPersists:
    """broadcast_reminder() must persist the reminder message to session history."""

    async def test_persists_to_single_active_session(self):
        """Reminder is saved to the one active WebSocket session."""
        reminder = {"id": "r1", "text": "call mom"}

        mock_manager = MagicMock()
        mock_manager.add_to_session = AsyncMock()

        mock_ws_adapter = MagicMock()
        mock_ws_adapter.broadcast = AsyncMock()
        mock_ws_adapter._connections = {"abc123": MagicMock()}

        with (
            patch(
                "pocketpaw.dashboard_lifecycle.ws_adapter",
                mock_ws_adapter,
            ),
            patch(
                "pocketpaw.dashboard_lifecycle.active_connections",
                [],
            ),
            patch(
                "pocketpaw.memory.get_memory_manager",
                return_value=mock_manager,
            ),
            patch("pocketpaw.bus.notifier.notify", new_callable=AsyncMock),
        ):
            from pocketpaw.dashboard_lifecycle import broadcast_reminder

            await broadcast_reminder(reminder)

        mock_manager.add_to_session.assert_awaited_once_with(
            session_key="websocket:abc123",
            role="assistant",
            content="Reminder: call mom",
            metadata={"reminder_id": "r1", "type": "reminder"},
        )

    async def test_persists_to_multiple_active_sessions(self):
        """Reminder is saved to every active WebSocket session."""
        reminder = {"id": "r2", "text": "team standup"}

        mock_manager = MagicMock()
        mock_manager.add_to_session = AsyncMock()

        mock_ws_adapter = MagicMock()
        mock_ws_adapter.broadcast = AsyncMock()
        mock_ws_adapter._connections = {
            "chat1": MagicMock(),
            "chat2": MagicMock(),
        }

        with (
            patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws_adapter),
            patch("pocketpaw.dashboard_lifecycle.active_connections", []),
            patch("pocketpaw.memory.get_memory_manager", return_value=mock_manager),
            patch("pocketpaw.bus.notifier.notify", new_callable=AsyncMock),
        ):
            from pocketpaw.dashboard_lifecycle import broadcast_reminder

            await broadcast_reminder(reminder)

        assert mock_manager.add_to_session.await_count == 2
        called_keys = {
            call.kwargs["session_key"] for call in mock_manager.add_to_session.await_args_list
        }
        assert called_keys == {"websocket:chat1", "websocket:chat2"}

    async def test_no_sessions_no_persist(self):
        """If no WebSocket connections are active, add_to_session is never called."""
        reminder = {"id": "r3", "text": "pick up groceries"}

        mock_manager = MagicMock()
        mock_manager.add_to_session = AsyncMock()

        mock_ws_adapter = MagicMock()
        mock_ws_adapter.broadcast = AsyncMock()
        mock_ws_adapter._connections = {}  # no active connections

        with (
            patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws_adapter),
            patch("pocketpaw.dashboard_lifecycle.active_connections", []),
            patch("pocketpaw.memory.get_memory_manager", return_value=mock_manager),
            patch("pocketpaw.bus.notifier.notify", new_callable=AsyncMock),
        ):
            from pocketpaw.dashboard_lifecycle import broadcast_reminder

            await broadcast_reminder(reminder)

        mock_manager.add_to_session.assert_not_awaited()

    async def test_persist_failure_does_not_prevent_broadcast(self):
        """If session persistence raises, the WS broadcast still succeeds."""
        reminder = {"id": "r4", "text": "water plants"}

        mock_manager = MagicMock()
        mock_manager.add_to_session = AsyncMock(side_effect=RuntimeError("disk full"))

        mock_ws_adapter = MagicMock()
        mock_ws_adapter.broadcast = AsyncMock()
        mock_ws_adapter._connections = {"chat99": MagicMock()}

        with (
            patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws_adapter),
            patch("pocketpaw.dashboard_lifecycle.active_connections", []),
            patch("pocketpaw.memory.get_memory_manager", return_value=mock_manager),
            patch("pocketpaw.bus.notifier.notify", new_callable=AsyncMock),
        ):
            from pocketpaw.dashboard_lifecycle import broadcast_reminder

            # Must not raise
            await broadcast_reminder(reminder)

        # Broadcast was still called
        mock_ws_adapter.broadcast.assert_awaited_once()

    async def test_reminder_content_format(self):
        """Persisted content matches what the frontend displays."""
        reminder = {"id": "r5", "text": "drink water"}

        mock_manager = MagicMock()
        mock_manager.add_to_session = AsyncMock()

        mock_ws_adapter = MagicMock()
        mock_ws_adapter.broadcast = AsyncMock()
        mock_ws_adapter._connections = {"sess1": MagicMock()}

        with (
            patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws_adapter),
            patch("pocketpaw.dashboard_lifecycle.active_connections", []),
            patch("pocketpaw.memory.get_memory_manager", return_value=mock_manager),
            patch("pocketpaw.bus.notifier.notify", new_callable=AsyncMock),
        ):
            from pocketpaw.dashboard_lifecycle import broadcast_reminder

            await broadcast_reminder(reminder)

        call_kwargs = mock_manager.add_to_session.await_args.kwargs
        assert call_kwargs["role"] == "assistant"
        assert call_kwargs["content"] == "Reminder: drink water"
        assert call_kwargs["metadata"]["type"] == "reminder"
        assert call_kwargs["metadata"]["reminder_id"] == "r5"

    async def test_notifier_uses_reminder_content(self):
        """The channel notifier is called with the same 'Reminder: …' text."""
        reminder = {"id": "r6", "text": "take medicine"}

        mock_manager = MagicMock()
        mock_manager.add_to_session = AsyncMock()

        mock_ws_adapter = MagicMock()
        mock_ws_adapter.broadcast = AsyncMock()
        mock_ws_adapter._connections = {}

        mock_notify = AsyncMock(return_value=0)

        with (
            patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws_adapter),
            patch("pocketpaw.dashboard_lifecycle.active_connections", []),
            patch("pocketpaw.memory.get_memory_manager", return_value=mock_manager),
            patch("pocketpaw.bus.notifier.notify", mock_notify),
        ):
            from pocketpaw.dashboard_lifecycle import broadcast_reminder

            await broadcast_reminder(reminder)

        mock_notify.assert_awaited_once_with("Reminder: take medicine")
