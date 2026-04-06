"""Tests for Microsoft Teams Channel Adapter — Sprint 22.

botbuilder-core is mocked since it's an optional dependency.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock botbuilder before importing the adapter
mock_bb_core = MagicMock()
mock_bb_schema = MagicMock()
mock_bb_aiohttp = MagicMock()
sys.modules.setdefault("botbuilder", MagicMock())
sys.modules.setdefault("botbuilder.core", mock_bb_core)
sys.modules.setdefault("botbuilder.schema", mock_bb_schema)
sys.modules.setdefault("botbuilder.integration.aiohttp", mock_bb_aiohttp)


from pocketpaw.bus.adapters.teams_adapter import TeamsAdapter  # noqa: E402
from pocketpaw.bus.events import Channel, OutboundMessage  # noqa: E402


class TestTeamsAdapterInit:
    def test_defaults(self):
        adapter = TeamsAdapter()
        assert adapter.app_id == ""
        assert adapter.app_password == ""
        assert adapter.channel == Channel.TEAMS
        assert adapter.webhook_port == 3978

    def test_custom_config(self):
        adapter = TeamsAdapter(
            app_id="app-123",
            app_password="secret",
            allowed_tenant_ids=["tenant-1"],
            webhook_port=4000,
        )
        assert adapter.app_id == "app-123"
        assert adapter.allowed_tenant_ids == ["tenant-1"]
        assert adapter.webhook_port == 4000


class TestTeamsAdapterProcessActivity:
    async def test_process_message(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        # Mock ActivityTypes
        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="Hello Teams!",
            from_property=SimpleNamespace(id="user-1"),
            conversation=SimpleNamespace(id="conv-1"),
            channel_data=None,
            id="act-1",
            service_url="https://smba.trafficmanager.net",
        )

        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Teams!"
        assert call_args.sender_id == "user-1"
        assert call_args.chat_id == "conv-1"
        assert call_args.channel == Channel.TEAMS

    async def test_skip_non_message_activity(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="typing",  # not a message
            text="",
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_empty_text_skipped(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=None,
            id="act",
            service_url="",
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_tenant_filter(self):
        adapter = TeamsAdapter(
            app_id="app",
            app_password="pw",
            allowed_tenant_ids=["tenant-ok"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="blocked",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=SimpleNamespace(tenant={"id": "tenant-bad"}),
            id="act",
            service_url="",
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_not_called()


class TestTeamsAdapterSend:
    async def test_send_normal_message(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()  # BotFrameworkAdapter mock

        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="conv-1",
            content="Hello!",
        )
        await adapter.send(msg)
        # Should not raise, just log

    async def test_send_stream_accumulates(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()

        chunk1 = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter.send(chunk1)
        await adapter.send(chunk2)
        assert adapter._buffers.get("c1") == "Hello World!"

    async def test_send_stream_end_flushes(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()

        adapter._buffers["c1"] = "accumulated text"

        end = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)
        assert "c1" not in adapter._buffers

    async def test_send_empty_skipped(self):
        adapter = TeamsAdapter()
        adapter._adapter = MagicMock()

        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="   ",
        )
        await adapter.send(msg)
        # Should not raise

    async def test_send_without_adapter(self):
        adapter = TeamsAdapter()
        # _adapter is None
        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="test",
        )
        await adapter.send(msg)  # should not raise


class TestTeamsAdapterErrorRecovery:
    """Tests for error recovery — network errors, auth failures, API errors."""

    async def test_send_exception_caught(self):
        """Exceptions during send are caught and don't propagate."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()

        # Make _send_text raise an exception
        async def failing_send(*args, **kwargs):
            raise Exception("Network error")

        adapter._send_text = failing_send

        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="conv-1",
            content="test",
        )
        # Should not raise — error is caught in send()
        await adapter.send(msg)

    async def test_process_activity_exception_caught(self):
        """Exceptions in _process_activity are handled gracefully."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock(side_effect=RuntimeError("Bus error"))

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="trigger error",
            from_property=SimpleNamespace(id="user-1"),
            conversation=SimpleNamespace(id="conv-1"),
            channel_data=None,
            id="act-1",
            service_url="",
            attachments=None,
        )

        turn_ctx = SimpleNamespace(activity=activity)
        # Exception propagates from _process_activity;
        # the webhook handler catches it at the outer level
        with pytest.raises(RuntimeError, match="Bus error"):
            await adapter._process_activity(turn_ctx)

    async def test_webhook_handler_invalid_json(self):
        """Webhook handler handles invalid JSON gracefully."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()

        # Create a mock request that raises on json()
        mock_request = MagicMock()
        mock_request.json = AsyncMock(side_effect=ValueError("Invalid JSON"))
        mock_request.headers = {"Authorization": ""}

        # Should return 500 error, not raise
        response = await adapter._handle_webhook(mock_request)
        assert response.status == 500


class TestTeamsAdapterBusIntegration:
    """Tests for MessageBus integration."""

    async def test_bus_outbound_subscription(self):
        """Adapter receives outbound messages from bus subscription."""
        from pocketpaw.bus.queue import MessageBus

        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MessageBus()

        # Mock the webhook server to not actually run
        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()
        adapter.send = AsyncMock()

        await adapter.start(bus)

        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="conv-1",
            content="response",
        )
        await bus.publish_outbound(msg)

        adapter.send.assert_called_once_with(msg)
        await adapter.stop()

    async def test_inbound_message_published(self):
        """Inbound messages are correctly published to bus."""
        from pocketpaw.bus.events import InboundMessage
        from pocketpaw.bus.queue import MessageBus

        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)

        msg = InboundMessage(
            channel=Channel.TEAMS,
            sender_id="user-1",
            chat_id="conv-1",
            content="test message",
        )
        await adapter._publish_inbound(msg)

        assert bus.inbound_pending() == 1
        consumed = await bus.consume_inbound()
        assert consumed.content == "test message"
        assert consumed.channel == Channel.TEAMS

        await adapter.stop()

    async def test_stop_unsubscribes_from_bus(self):
        """Stop properly unsubscribes from bus outbound events."""
        from pocketpaw.bus.queue import MessageBus

        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MessageBus()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()
        adapter.send = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()

        # After stop, outbound messages should not reach the adapter
        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="conv-1",
            content="after stop",
        )
        await bus.publish_outbound(msg)

        adapter.send.assert_not_called()


class TestTeamsAdapterLifecycle:
    """Tests for adapter start/stop lifecycle."""

    async def test_start_sets_running_flag(self):
        """Start sets the _running flag."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()

        await adapter.start(bus)
        assert adapter._running is True
        await adapter.stop()

    async def test_stop_clears_running_flag(self):
        """Stop clears the _running flag."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()
        assert adapter._running is False

    async def test_start_without_credentials_logs_error(self):
        """Start without app_id/password logs error but doesn't crash."""
        adapter = TeamsAdapter()  # no credentials
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        await adapter.start(bus)
        # Should not crash, _server_task should be None
        assert adapter._server_task is None
        await adapter.stop()

    async def test_double_stop_is_safe(self):
        """Calling stop twice doesn't raise errors."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        adapter._on_start = AsyncMock()
        adapter._on_stop = AsyncMock()

        await adapter.start(bus)
        await adapter.stop()
        # Second stop should not raise
        await adapter.stop()

    async def test_stop_cancels_server_task(self):
        """Stop cancels the webhook server task."""
        import asyncio

        adapter = TeamsAdapter(app_id="app", app_password="pw")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        # Create a mock server task
        async def mock_server():
            await asyncio.sleep(100)

        adapter._server_task = asyncio.create_task(mock_server())
        adapter._running = True
        adapter._bus = bus

        await adapter.stop()

        assert adapter._running is False
        assert adapter._server_task.done() or adapter._server_task.cancelled()


class TestTeamsAdapterTenantFilter:
    """Additional tests for tenant filtering."""

    async def test_tenant_filter_allows_valid_tenant(self):
        """Messages from allowed tenant are processed."""
        adapter = TeamsAdapter(
            app_id="app",
            app_password="pw",
            allowed_tenant_ids=["tenant-ok"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="allowed",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=SimpleNamespace(tenant={"id": "tenant-ok"}),
            id="act",
            service_url="",
            attachments=None,
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_called_once()

    async def test_tenant_filter_with_tenant_object(self):
        """Tenant filter handles tenant object with id attribute."""
        adapter = TeamsAdapter(
            app_id="app",
            app_password="pw",
            allowed_tenant_ids=["tenant-ok"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        # Tenant as object with id attribute
        tenant_obj = SimpleNamespace(id="tenant-ok")
        activity = SimpleNamespace(
            type="message",
            text="allowed",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=SimpleNamespace(tenant=tenant_obj),
            id="act",
            service_url="",
            attachments=None,
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_called_once()

    async def test_no_tenant_filter_allows_all(self):
        """When allowed_tenant_ids is empty, all tenants are allowed."""
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="any tenant",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=SimpleNamespace(tenant={"id": "any-tenant"}),
            id="act",
            service_url="",
            attachments=None,
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_called_once()
