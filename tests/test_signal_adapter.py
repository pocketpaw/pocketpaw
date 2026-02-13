"""Tests for Signal Channel Adapter — Sprint 20."""

from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.bus.adapters.signal_adapter import SignalAdapter
from pocketclaw.bus.events import Channel, OutboundMessage


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
