"""Tests for Matrix Channel Adapter — Sprint 21.

matrix-nio is mocked since it's an optional dependency.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Mock nio before importing the adapter
mock_nio = MagicMock()
mock_nio.AsyncClient = MagicMock
mock_nio.RoomMessageText = type("RoomMessageText", (), {})
mock_nio.RoomSendResponse = type("RoomSendResponse", (), {"event_id": "evt1"})
sys.modules.setdefault("nio", mock_nio)


from pocketpaw.bus.adapters.matrix_adapter import MatrixAdapter  # noqa: E402
from pocketpaw.bus.events import Channel, OutboundMessage  # noqa: E402


class TestMatrixAdapterInit:
    def test_defaults(self):
        adapter = MatrixAdapter()
        assert adapter.homeserver == ""
        assert adapter.user_id == ""
        assert adapter.channel == Channel.MATRIX
        assert adapter.device_id == "POCKETPAW"

    def test_custom_config(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            access_token="tok123",
            allowed_room_ids=["!room:matrix.org"],
        )
        assert adapter.homeserver == "https://matrix.org"
        assert adapter.user_id == "@bot:matrix.org"
        assert adapter.access_token == "tok123"
        assert adapter.allowed_room_ids == ["!room:matrix.org"]


class TestMatrixAdapterMessage:
    async def test_handle_valid_message(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="Hello Matrix!",
            event_id="$event1",
        )

        await adapter._on_message(room, event)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Matrix!"
        assert call_args.sender_id == "@user:matrix.org"
        assert call_args.chat_id == "!room:matrix.org"
        assert call_args.channel == Channel.MATRIX

    async def test_skip_own_messages(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!room:matrix.org")
        event = SimpleNamespace(
            sender="@bot:matrix.org",  # own message
            body="echo",
            event_id="$evt",
        )

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_unauthorized_room_filtered(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            allowed_room_ids=["!allowed:matrix.org"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!other:matrix.org")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="blocked",
            event_id="$evt",
        )

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_empty_message_skipped(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!room:matrix.org")
        event = SimpleNamespace(sender="@user:matrix.org", body="", event_id="$evt")

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_initial_sync_messages_skipped(self):
        """Messages during initial sync (historical) are ignored."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()
        adapter._initial_sync_done = False  # still syncing

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="old message from history",
            event_id="$old",
        )

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_initial_sync_media_messages_skipped(self):
        """Media messages during initial sync are ignored."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()
        adapter._initial_sync_done = False

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="photo.jpg",
            event_id="$old_media",
            url="mxc://matrix.org/abc123",
            source={},
        )

        await adapter._on_media_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_callback_exception_does_not_propagate(self):
        """Errors in _on_message are caught, not propagated to sync loop."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock(side_effect=RuntimeError("bus down"))
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="trigger error",
            event_id="$err",
        )

        # Should not raise — error is caught internally
        await adapter._on_message(room, event)


class TestMatrixAdapterSend:
    async def test_send_normal_message(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()

        # Create a proper mock response class
        class FakeRoomSendResponse:
            event_id = "$sent1"

        resp = FakeRoomSendResponse()
        mock_client.room_send = AsyncMock(return_value=resp)
        adapter._client = mock_client

        # Patch nio.RoomSendResponse so isinstance check works
        mock_nio.RoomSendResponse = FakeRoomSendResponse

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room:matrix.org",
            content="Hello!",
        )
        await adapter.send(msg)
        mock_client.room_send.assert_called_once()

    async def test_send_stream_accumulates(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        adapter._client = mock_client

        # Send chunks
        chunk1 = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="World!",
            is_stream_chunk=True,
        )

        await adapter.send(chunk1)
        await adapter.send(chunk2)

        assert adapter._buffers.get("!r") == "Hello World!"

    async def test_send_stream_end_flushes(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        resp = SimpleNamespace(event_id="$sent")
        mock_client.room_send = AsyncMock(return_value=resp)
        adapter._client = mock_client

        adapter._buffers["!r"] = "accumulated text"

        end = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)
        mock_client.room_send.assert_called_once()

    async def test_send_empty_skipped(self):
        adapter = MatrixAdapter()
        mock_client = AsyncMock()
        adapter._client = mock_client

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="   ",
        )
        await adapter.send(msg)
        mock_client.room_send.assert_not_called()

    async def test_send_without_client(self):
        adapter = MatrixAdapter()
        # _client is None
        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="test",
        )
        await adapter.send(msg)  # should not raise


class TestMatrixAdapterErrorRecovery:
    """Tests for error recovery — network errors, auth failures, API errors."""

    async def test_send_exception_caught(self):
        """Exceptions during send are caught and don't propagate."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(side_effect=Exception("Network error"))
        adapter._client = mock_client

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room:matrix.org",
            content="test",
        )
        # Should not raise
        await adapter.send(msg)

    async def test_send_returns_error_response(self):
        """Non-RoomSendResponse is logged as error."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        error_resp = SimpleNamespace(message="Rate limited")
        mock_client.room_send = AsyncMock(return_value=error_resp)
        adapter._client = mock_client

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room:matrix.org",
            content="test",
        )
        # Should not raise — error is logged
        await adapter.send(msg)

    async def test_edit_message_exception_caught(self):
        """Exceptions during message edit are caught."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(side_effect=Exception("Edit failed"))
        adapter._client = mock_client

        # Should not raise
        await adapter._edit_message("!room", "$evt", "new text")

    async def test_on_message_exception_caught(self):
        """Exceptions in _on_message are caught, not propagated."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock(side_effect=RuntimeError("Bus error"))
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="trigger error",
            event_id="$err",
        )

        # Should not raise — error is caught internally
        await adapter._on_message(room, event)

    async def test_on_media_message_exception_caught(self):
        """Exceptions in _on_media_message are caught."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock(side_effect=RuntimeError("Bus error"))
        adapter._initial_sync_done = True

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="photo.jpg",
            event_id="$media",
            url=None,  # No mxc URL
        )

        # Should not raise
        await adapter._on_media_message(room, event)


class TestMatrixAdapterBusIntegration:
    """Tests for MessageBus integration."""

    async def test_bus_outbound_subscription(self):
        """Adapter receives outbound messages from bus subscription."""
        from pocketpaw.bus.queue import MessageBus

        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        bus = MessageBus()

        # Mock the sync loop to not actually run
        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()
        adapter.send = AsyncMock()

        await adapter.start(bus)

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room:matrix.org",
            content="response",
        )
        await bus.publish_outbound(msg)

        adapter.send.assert_called_once_with(msg)
        await adapter.stop()

    async def test_inbound_message_published(self):
        """Inbound messages are correctly published to bus."""
        from pocketpaw.bus.events import InboundMessage
        from pocketpaw.bus.queue import MessageBus

        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)

        msg = InboundMessage(
            channel=Channel.MATRIX,
            sender_id="@user:matrix.org",
            chat_id="!room:matrix.org",
            content="test message",
        )
        await adapter._publish_inbound(msg)

        assert bus.inbound_pending() == 1
        consumed = await bus.consume_inbound()
        assert consumed.content == "test message"
        assert consumed.channel == Channel.MATRIX

        await adapter.stop()

    async def test_stop_unsubscribes_from_bus(self):
        """Stop properly unsubscribes from bus outbound events."""
        from pocketpaw.bus.queue import MessageBus

        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()
        adapter.send = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()

        # After stop, outbound messages should not reach the adapter
        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room:matrix.org",
            content="after stop",
        )
        await bus.publish_outbound(msg)

        adapter.send.assert_not_called()


class TestMatrixAdapterLifecycle:
    """Tests for adapter start/stop lifecycle."""

    async def test_start_sets_running_flag(self):
        """Start sets the _running flag."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            access_token="tok123",
        )
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        # Mock nio to avoid actual connection
        adapter._on_start = AsyncMock()

        await adapter.start(bus)
        assert adapter._running is True
        await adapter.stop()

    async def test_stop_clears_running_flag(self):
        """Stop clears the _running flag."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()
        assert adapter._running is False

    async def test_start_without_homeserver_logs_error(self):
        """Start without homeserver logs error but doesn't crash."""
        adapter = MatrixAdapter()  # no homeserver
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        await adapter.start(bus)
        # Should not crash, _sync_task should be None
        assert adapter._sync_task is None
        await adapter.stop()

    async def test_double_stop_is_safe(self):
        """Calling stop twice doesn't raise errors."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()
        # Second stop should not raise
        await adapter.stop()


class TestMatrixAdapterStreaming:
    """Additional streaming tests with rate limiting."""

    async def test_stream_with_edit_event_id(self):
        """Streaming edits existing message when event_id is available."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()

        class FakeRoomSendResponse:
            event_id = "$initial"

        mock_client.room_send = AsyncMock(return_value=FakeRoomSendResponse())
        adapter._client = mock_client

        # Prime with an existing edit event
        adapter._edit_event_ids["!room"] = "$existing"
        adapter._buffers["!room"] = "Previous "
        adapter._last_edit_time["!room"] = 0  # Long ago — should allow edit

        chunk = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room",
            content="text",
            is_stream_chunk=True,
        )
        await adapter.send(chunk)

        # Buffer should be updated
        assert adapter._buffers["!room"] == "Previous text"

    async def test_stream_end_clears_edit_state(self):
        """Stream end clears edit event ID and timing state."""
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$sent"))
        adapter._client = mock_client

        # Set up streaming state
        adapter._buffers["!room"] = "Final text"
        adapter._edit_event_ids["!room"] = "$evt"
        adapter._last_edit_time["!room"] = 123.456

        end = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)

        # All state should be cleared
        assert "!room" not in adapter._buffers
        assert "!room" not in adapter._edit_event_ids
        assert "!room" not in adapter._last_edit_time
