"""Tests for Signal Channel Adapter — Sprint 20."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pocketpaw.bus.adapters.signal_adapter import SignalAdapter
from pocketpaw.bus.events import Channel, OutboundMessage


class TestSignalAdapterInit:
    def test_defaults(self):
        adapter = SignalAdapter()
        assert adapter.api_url == "http://localhost:8080"
        assert adapter.phone_number == ""
        assert adapter.allowed_phone_numbers == []
        assert adapter.channel == Channel.SIGNAL

    def test_custom_config(self):
        adapter = SignalAdapter(
            api_url="http://signal:9090/",
            phone_number="+1234567890",
            allowed_phone_numbers=["+1111111111"],
        )
        assert adapter.api_url == "http://signal:9090"
        assert adapter.phone_number == "+1234567890"
        assert adapter.allowed_phone_numbers == ["+1111111111"]


class TestSignalAdapterStartStop:
    async def test_start_sets_running(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        # Patch _poll_loop so it doesn't actually run
        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            await adapter.start(bus)
            assert adapter._running is True
            assert adapter._http is not None

            await adapter.stop()
            assert adapter._running is False

    async def test_start_without_phone_number(self):
        adapter = SignalAdapter()  # no phone_number
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        await adapter.start(bus)
        assert adapter._running is True
        # Should log error but not crash
        await adapter.stop()


class TestSignalAdapterHandleMessage:
    async def test_handle_valid_message(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        msg_data = {
            "envelope": {
                "source": "+9876543210",
                "dataMessage": {"message": "Hello Signal!"},
                "timestamp": 1234567890,
            }
        }
        await adapter._handle_message(msg_data)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Signal!"
        assert call_args.sender_id == "+9876543210"
        assert call_args.channel == Channel.SIGNAL

    async def test_handle_message_no_content(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        msg_data = {"envelope": {"source": "+9876543210", "dataMessage": {}}}
        await adapter._handle_message(msg_data)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_handle_message_no_source(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        msg_data = {"envelope": {"dataMessage": {"message": "test"}}}
        await adapter._handle_message(msg_data)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_handle_message_unauthorized(self):
        adapter = SignalAdapter(
            phone_number="+1234567890",
            allowed_phone_numbers=["+1111111111"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        msg_data = {
            "envelope": {
                "source": "+9999999999",  # not allowed
                "dataMessage": {"message": "blocked"},
            }
        }
        await adapter._handle_message(msg_data)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_handle_message_sourceNumber_fallback(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        msg_data = {
            "envelope": {
                "sourceNumber": "+5555555555",
                "dataMessage": {"message": "via sourceNumber"},
            }
        }
        await adapter._handle_message(msg_data)
        adapter._bus.publish_inbound.assert_called_once()


class TestSignalAdapterSend:
    async def test_send_normal_message(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock(return_value=MagicMock(status_code=200))

        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+9876543210",
            content="Hello!",
        )
        await adapter.send(msg)

        adapter._http.post.assert_called_once()
        call_kwargs = adapter._http.post.call_args
        assert call_kwargs[1]["json"]["message"] == "Hello!"
        assert call_kwargs[1]["json"]["recipients"] == ["+9876543210"]

    async def test_send_stream_chunks(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock(return_value=MagicMock(status_code=200))

        # Send stream chunks
        chunk1 = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="World!",
            is_stream_chunk=True,
        )
        end = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="",
            is_stream_end=True,
        )

        await adapter.send(chunk1)
        await adapter.send(chunk2)
        adapter._http.post.assert_not_called()  # buffered

        await adapter.send(end)
        adapter._http.post.assert_called_once()
        assert "Hello World!" in adapter._http.post.call_args[1]["json"]["message"]

    async def test_send_empty_message_skipped(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock()

        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="   ",
        )
        await adapter.send(msg)
        adapter._http.post.assert_not_called()

    async def test_send_without_http_client(self):
        adapter = SignalAdapter(phone_number="+1234567890")
        # _http is None
        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="test",
        )
        await adapter.send(msg)  # should not raise


class TestSignalAdapterErrorRecovery:
    """Tests for error recovery — network errors, auth failures, API errors."""

    async def test_send_api_error_logged(self):
        """HTTP 4xx/5xx errors are logged but don't raise."""
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock(
            return_value=MagicMock(status_code=500, text="Internal Server Error")
        )

        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="test",
        )
        # Should not raise
        await adapter.send(msg)
        adapter._http.post.assert_called_once()

    async def test_send_auth_error_logged(self):
        """HTTP 401/403 auth errors are logged but don't raise."""
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock(
            return_value=MagicMock(status_code=401, text="Unauthorized")
        )

        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="test",
        )
        await adapter.send(msg)
        adapter._http.post.assert_called_once()

    async def test_send_rate_limit_error_logged(self):
        """HTTP 429 rate limit errors are logged but don't raise."""
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock(
            return_value=MagicMock(status_code=429, text="Rate limited")
        )

        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="test",
        )
        await adapter.send(msg)
        adapter._http.post.assert_called_once()

    async def test_send_network_exception_caught(self):
        """Network exceptions are caught and don't propagate."""
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._http = AsyncMock()
        adapter._http.post = AsyncMock(side_effect=Exception("Connection refused"))

        msg = OutboundMessage(
            channel=Channel.SIGNAL,
            chat_id="+111",
            content="test",
        )
        # Should not raise
        await adapter.send(msg)

    async def test_handle_message_exception_caught(self):
        """Exceptions from _publish_inbound propagate to the poll loop (which catches them)."""
        adapter = SignalAdapter(phone_number="+1234567890")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock(side_effect=RuntimeError("Bus error"))

        msg_data = {
            "envelope": {
                "source": "+9876543210",
                "dataMessage": {"message": "trigger error"},
            }
        }
        # Exception propagates from _handle_message; the poll loop catches it at the outer level
        with pytest.raises(RuntimeError, match="Bus error"):
            await adapter._handle_message(msg_data)


class TestSignalAdapterBusIntegration:
    """Tests for MessageBus integration."""

    async def test_bus_outbound_subscription(self):
        """Adapter receives outbound messages from bus subscription."""
        from pocketpaw.bus.queue import MessageBus

        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MessageBus()

        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            adapter.send = AsyncMock()
            await adapter.start(bus)

            msg = OutboundMessage(
                channel=Channel.SIGNAL,
                chat_id="+111",
                content="response",
            )
            await bus.publish_outbound(msg)

            adapter.send.assert_called_once_with(msg)
            await adapter.stop()

    async def test_inbound_message_published(self):
        """Inbound messages are correctly published to bus."""
        from pocketpaw.bus.events import InboundMessage
        from pocketpaw.bus.queue import MessageBus

        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MessageBus()

        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            await adapter.start(bus)

            msg = InboundMessage(
                channel=Channel.SIGNAL,
                sender_id="+111",
                chat_id="+111",
                content="test message",
            )
            await adapter._publish_inbound(msg)

            assert bus.inbound_pending() == 1
            consumed = await bus.consume_inbound()
            assert consumed.content == "test message"

            await adapter.stop()

    async def test_stop_unsubscribes_from_bus(self):
        """Stop properly unsubscribes from bus outbound events."""
        from pocketpaw.bus.queue import MessageBus

        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MessageBus()

        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            adapter.send = AsyncMock()
            await adapter.start(bus)
            await adapter.stop()

            # After stop, outbound messages should not reach the adapter
            msg = OutboundMessage(
                channel=Channel.SIGNAL,
                chat_id="+111",
                content="after stop",
            )
            await bus.publish_outbound(msg)

            # send should only have been called once (for the first message if any)
            # In this case it shouldn't be called at all since we stopped before sending
            adapter.send.assert_not_called()


class TestSignalAdapterLifecycle:
    """Tests for adapter start/stop lifecycle."""

    async def test_start_creates_http_client_and_poll_task(self):
        """Start initializes HTTP client and creates poll task."""
        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            await adapter.start(bus)

            assert adapter._running is True
            assert adapter._http is not None
            assert adapter._poll_task is not None

            await adapter.stop()

    async def test_stop_cancels_poll_task(self):
        """Stop cancels the poll task gracefully."""
        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            await adapter.start(bus)
            poll_task = adapter._poll_task

            await adapter.stop()

            assert adapter._running is False
            # Poll task should be cancelled or completed
            assert poll_task.done() or poll_task.cancelled()

    async def test_stop_closes_http_client(self):
        """Stop closes the HTTP client."""
        import httpx

        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        with patch("httpx.AsyncClient", return_value=mock_http_client), \
             patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            await adapter.start(bus)

            await adapter.stop()

            # HTTP client should be closed
            mock_http_client.aclose.assert_called_once()

    async def test_double_stop_is_safe(self):
        """Calling stop twice doesn't raise errors."""
        adapter = SignalAdapter(phone_number="+1234567890")
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        with patch.object(adapter, "_poll_loop", new_callable=AsyncMock):
            await adapter.start(bus)
            await adapter.stop()
            # Second stop should not raise
            await adapter.stop()

    async def test_start_without_phone_logs_error(self):
        """Start without phone_number logs error but doesn't crash."""
        adapter = SignalAdapter()  # no phone_number
        bus = MagicMock()
        bus.subscribe_outbound = MagicMock()
        bus.unsubscribe_outbound = MagicMock()

        await adapter.start(bus)
        # Should not crash, _poll_task should be None
        assert adapter._poll_task is None
        await adapter.stop()
