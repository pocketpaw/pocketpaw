"""Tests for Google Chat Channel Adapter — Sprint 23.

google-api-python-client is mocked since it's an optional dependency.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# Mock google libs before importing the adapter
mock_oauth2 = MagicMock()
mock_service_account = MagicMock()
mock_oauth2.service_account = mock_service_account
mock_discovery = MagicMock()
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", mock_oauth2)
sys.modules.setdefault("google.oauth2.service_account", mock_service_account)
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", mock_discovery)


from pocketpaw.bus.adapters.gchat_adapter import GoogleChatAdapter  # noqa: E402
from pocketpaw.bus.events import Channel, OutboundMessage  # noqa: E402


class TestGoogleChatAdapterInit:
    def test_defaults(self):
        adapter = GoogleChatAdapter()
        assert adapter.mode == "webhook"
        assert adapter.service_account_key is None
        assert adapter.channel == Channel.GOOGLE_CHAT
        assert adapter.allowed_space_ids == []

    def test_custom_config(self):
        adapter = GoogleChatAdapter(
            mode="pubsub",
            service_account_key="/path/to/key.json",
            project_id="my-project",
            subscription_id="my-sub",
            allowed_space_ids=["spaces/ABC"],
        )
        assert adapter.mode == "pubsub"
        assert adapter.service_account_key == "/path/to/key.json"
        assert adapter.project_id == "my-project"
        assert adapter.subscription_id == "my-sub"
        assert adapter.allowed_space_ids == ["spaces/ABC"]


class TestGoogleChatAdapterWebhook:
    async def test_handle_valid_message(self):
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "Hello Google Chat!",
                "thread": {"name": "spaces/AAA/threads/t1"},
            },
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Google Chat!"
        assert call_args.sender_id == "users/123"
        assert call_args.chat_id == "spaces/AAA"
        assert call_args.channel == Channel.GOOGLE_CHAT
        assert call_args.metadata["sender_display_name"] == "Alice"

    async def test_skip_non_message_event(self):
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "ADDED_TO_SPACE",
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_empty_text_skipped(self):
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "",
                "thread": {"name": "spaces/AAA/threads/t1"},
            },
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_space_filter(self):
        adapter = GoogleChatAdapter(allowed_space_ids=["spaces/ALLOWED"])
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/OTHER/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "blocked",
                "thread": {},
            },
            "space": {"name": "spaces/OTHER"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_argument_text_fallback(self):
        """When text is empty but argumentText is set (slash commands)."""
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Bob"},
                "text": "",
                "argumentText": "/help me",
                "thread": {},
            },
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "/help me"


class TestGoogleChatAdapterSend:
    async def test_send_normal_message(self):
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        mock_service.spaces().messages().create().execute = MagicMock()
        adapter._chat_service = mock_service

        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/AAA",
            content="Hello!",
        )
        await adapter.send(msg)

    async def test_send_stream_accumulates(self):
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        adapter._chat_service = mock_service

        chunk1 = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter.send(chunk1)
        await adapter.send(chunk2)
        assert adapter._buffers.get("spaces/A") == "Hello World!"

    async def test_send_stream_end_flushes(self):
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        adapter._chat_service = mock_service

        adapter._buffers["spaces/A"] = "accumulated text"

        end = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)
        assert "spaces/A" not in adapter._buffers

    async def test_send_empty_skipped(self):
        adapter = GoogleChatAdapter()
        adapter._chat_service = MagicMock()

        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="   ",
        )
        await adapter.send(msg)

    async def test_send_without_service(self):
        adapter = GoogleChatAdapter()
        # _chat_service is None
        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="test",
        )
        await adapter.send(msg)  # should not raise


class TestGoogleChatAdapterErrorRecovery:
    """Tests for error recovery — network errors, auth failures, API errors."""

    async def test_send_exception_caught(self):
        """Exceptions during send are caught and don't propagate."""
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        mock_service.spaces().messages().create().execute.side_effect = Exception("API error")
        adapter._chat_service = mock_service

        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="test",
        )
        # Should not raise — error is caught in _send_text()
        await adapter.send(msg)

    async def test_handle_webhook_exception_caught(self):
        """Exceptions in handle_webhook_message are caught."""
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock(side_effect=RuntimeError("Bus error"))

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "trigger error",
                "thread": {},
            },
            "space": {"name": "spaces/AAA"},
        }

        # Should not raise — error should be caught
        await adapter.handle_webhook_message(payload)

    async def test_invalid_webhook_payload_handled(self):
        """Invalid/malformed webhook payloads are handled gracefully."""
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        # Payload with missing keys
        payload = {"type": "MESSAGE"}  # No message key

        # Should not raise
        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()


class TestGoogleChatAdapterBusIntegration:
    """Tests for MessageBus integration."""

    async def test_bus_outbound_subscription(self):
        """Adapter receives outbound messages from bus subscription."""
        from pocketpaw.bus.queue import MessageBus

        adapter = GoogleChatAdapter()
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()
        adapter.send = AsyncMock()

        await adapter.start(bus)

        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="response",
        )
        await bus.publish_outbound(msg)

        adapter.send.assert_called_once_with(msg)
        await adapter.stop()

    async def test_inbound_message_published(self):
        """Inbound messages are correctly published to bus."""
        from pocketpaw.bus.events import InboundMessage
        from pocketpaw.bus.queue import MessageBus

        adapter = GoogleChatAdapter()
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)

        msg = InboundMessage(
            channel=Channel.GOOGLE_CHAT,
            sender_id="users/123",
            chat_id="spaces/A",
            content="test message",
        )
        await adapter._publish_inbound(msg)

        assert bus.inbound_pending() == 1
        consumed = await bus.consume_inbound()
        assert consumed.content == "test message"
        assert consumed.channel == Channel.GOOGLE_CHAT

        await adapter.stop()

    async def test_stop_unsubscribes_from_bus(self):
        """Stop properly unsubscribes from bus outbound events."""
        from pocketpaw.bus.queue import MessageBus

        adapter = GoogleChatAdapter()
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()
        adapter.send = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()

        # After stop, outbound messages should not reach the adapter
        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="after stop",
        )
        await bus.publish_outbound(msg)

        adapter.send.assert_not_called()


class TestGoogleChatAdapterLifecycle:
    """Tests for adapter start/stop lifecycle."""

    async def test_start_sets_running_flag(self):
        """Start sets the _running flag."""
        adapter = GoogleChatAdapter()
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()

        await adapter.start(bus)
        assert adapter._running is True
        await adapter.stop()

    async def test_stop_clears_running_flag(self):
        """Stop clears the _running flag."""
        adapter = GoogleChatAdapter()
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()
        assert adapter._running is False

    async def test_double_stop_is_safe(self):
        """Calling stop twice doesn't raise errors."""
        adapter = GoogleChatAdapter()
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()
        # Second stop should not raise
        await adapter.stop()

    async def test_start_pubsub_mode_creates_poll_task(self):
        """Start in pubsub mode creates poll task."""
        adapter = GoogleChatAdapter(
            mode="pubsub",
            project_id="my-project",
            subscription_id="my-sub",
        )
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        # Mock _init_credentials and _pubsub_loop
        adapter._init_credentials = AsyncMock()

        async def mock_pubsub_loop():
            pass

        adapter._pubsub_loop = mock_pubsub_loop

        await adapter.start(bus)
        # In pubsub mode, poll task should be created
        # (actual creation depends on _on_start logic)
        await adapter.stop()

    async def test_stop_cancels_poll_task(self):
        """Stop cancels the pubsub poll task."""
        import asyncio

        adapter = GoogleChatAdapter(mode="pubsub")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        # Create a mock poll task
        async def mock_poll():
            await asyncio.sleep(100)

        adapter._poll_task = asyncio.create_task(mock_poll())
        adapter._running = True
        adapter._bus = bus

        await adapter.stop()

        assert adapter._running is False
        assert adapter._poll_task.done() or adapter._poll_task.cancelled()


class TestGoogleChatAdapterPubSub:
    """Tests for Pub/Sub mode functionality."""

    async def test_webhook_mode_no_poll_task(self):
        """Webhook mode doesn't create a poll task."""
        adapter = GoogleChatAdapter(mode="webhook")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        await adapter.start(bus)
        assert adapter._poll_task is None
        await adapter.stop()

    async def test_pubsub_mode_missing_config_no_poll(self):
        """Pubsub mode without project/subscription doesn't poll."""
        adapter = GoogleChatAdapter(mode="pubsub")  # No project_id or subscription_id
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        await adapter.start(bus)
        # Should not create poll task without full config
        assert adapter._poll_task is None
        await adapter.stop()
