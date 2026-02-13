# Tests for Slack Channel Adapter
# Created: 2026-02-06

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# --- Mock slack_bolt modules before importing the adapter ---

mock_slack_bolt = types.ModuleType("slack_bolt")
mock_async_app_module = types.ModuleType("slack_bolt.async_app")
mock_socket_module = types.ModuleType("slack_bolt.adapter")
mock_socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
mock_async_handler = types.ModuleType("slack_bolt.adapter.socket_mode.async_handler")


class MockAsyncApp:
    def __init__(self, **kwargs):
        self.token = kwargs.get("token")
        self.client = MagicMock()
        self.client.chat_postMessage = AsyncMock(return_value={"ts": "1234567890.123456"})
        self.client.chat_update = AsyncMock()
        self._event_handlers = {}
        self._command_handlers = {}

    def event(self, event_type):
        def decorator(func):
            self._event_handlers[event_type] = func
            return func

        return decorator

    def command(self, cmd_name):
        def decorator(func):
            self._command_handlers[cmd_name] = func
            return func

        return decorator


class MockAsyncSocketModeHandler:
    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def start_async(self):
        pass

    async def close_async(self):
        pass


mock_async_app_module.AsyncApp = MockAsyncApp
mock_async_handler.AsyncSocketModeHandler = MockAsyncSocketModeHandler

sys.modules.setdefault("slack_bolt", mock_slack_bolt)
sys.modules.setdefault("slack_bolt.async_app", mock_async_app_module)
sys.modules.setdefault("slack_bolt.adapter", mock_socket_module)
sys.modules.setdefault("slack_bolt.adapter.socket_mode", mock_socket_mode)
sys.modules.setdefault("slack_bolt.adapter.socket_mode.async_handler", mock_async_handler)

from pocketclaw.bus.adapters.slack_adapter import SlackAdapter
from pocketclaw.bus.events import Channel, OutboundMessage
from pocketclaw.bus.queue import MessageBus


@pytest.fixture
def adapter():
    return SlackAdapter(
        bot_token="xoxb-test",
        app_token="xapp-test",
        allowed_channel_ids=["C111", "C222"],
    )


@pytest.fixture
def bus():
    return MessageBus()


def test_channel_property(adapter):
    assert adapter.channel == Channel.SLACK


async def test_start_stop(adapter, bus):
    await adapter.start(bus)
    assert adapter._running is True
    assert adapter._slack_app is not None

    await adapter.stop()
    assert adapter._running is False


async def test_send_normal_message(adapter, bus):
    await adapter.start(bus)

    msg = OutboundMessage(
        channel=Channel.SLACK,
        chat_id="C111",
        content="Hello Slack!",
    )
    await adapter.send(msg)

    adapter._slack_app.client.chat_postMessage.assert_called_once_with(
        channel="C111",
        text="Hello Slack!",
    )


async def test_send_with_thread_ts(adapter, bus):
    await adapter.start(bus)

    msg = OutboundMessage(
        channel=Channel.SLACK,
        chat_id="C111",
        content="Threaded reply",
        metadata={"thread_ts": "1234567890.111"},
    )
    await adapter.send(msg)

    adapter._slack_app.client.chat_postMessage.assert_called_once_with(
        channel="C111",
        text="Threaded reply",
        thread_ts="1234567890.111",
    )


async def test_stream_buffering(adapter, bus):
    await adapter.start(bus)

    chunk = OutboundMessage(
        channel=Channel.SLACK,
        chat_id="C111",
        content="Hello ",
        is_stream_chunk=True,
    )
    await adapter.send(chunk)

    assert "C111" in adapter._buffers
    assert adapter._buffers["C111"]["text"] == "Hello "
    # First chunk triggers chat_postMessage with "..."
    adapter._slack_app.client.chat_postMessage.assert_called_once()


async def test_stream_flush_via_chat_update(adapter, bus):
    await adapter.start(bus)

    # Prime the buffer
    adapter._buffers["C111"] = {
        "ts": "1234567890.123456",
        "text": "Complete response",
        "thread_ts": None,
        "last_update": 0,
    }

    end_msg = OutboundMessage(
        channel=Channel.SLACK,
        chat_id="C111",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end_msg)

    assert "C111" not in adapter._buffers
    adapter._slack_app.client.chat_update.assert_called_once_with(
        channel="C111",
        ts="1234567890.123456",
        text="Complete response",
    )


async def test_channel_filtering(adapter, bus):
    """Messages from non-allowed channels are ignored."""
    await adapter.start(bus)

    event = {
        "channel": "C999",  # not in allowed list
        "user": "U123",
        "text": "hello",
        "channel_type": "im",
    }
    await adapter._handle_slack_event(event)

    # No message should be published
    assert bus.inbound_pending() == 0


async def test_mention_handler(adapter, bus):
    """app_mention events are processed."""
    await adapter.start(bus)

    event = {
        "channel": "C111",
        "user": "U123",
        "text": "<@BOT123> what is the weather",
        "ts": "123.456",
    }
    await adapter._handle_slack_event(event)

    assert bus.inbound_pending() == 1
    msg = await bus.consume_inbound()
    assert msg.content == "what is the weather"
    assert msg.channel == Channel.SLACK


async def test_dm_handler(adapter, bus):
    """DM events (channel_type=im) are handled."""
    adapter_open = SlackAdapter(
        bot_token="xoxb-test",
        app_token="xapp-test",
        allowed_channel_ids=[],  # No restrictions
    )
    await adapter_open.start(bus)

    event = {
        "channel": "D999",
        "user": "U123",
        "text": "hello bot",
        "channel_type": "im",
        "ts": "123.789",
    }
    await adapter_open._handle_slack_event(event)

    assert bus.inbound_pending() == 1
    msg = await bus.consume_inbound()
    assert msg.content == "hello bot"


async def test_thread_ts_in_metadata(adapter, bus):
    """thread_ts is passed through metadata."""
    await adapter.start(bus)

    event = {
        "channel": "C111",
        "user": "U123",
        "text": "reply in thread",
        "thread_ts": "123.000",
        "ts": "123.001",
    }
    await adapter._handle_slack_event(event)

    msg = await bus.consume_inbound()
    assert msg.metadata["thread_ts"] == "123.000"


async def test_bus_integration(bus):
    """Adapter receives outbound messages from bus subscription."""
    adapter = SlackAdapter(bot_token="xoxb-t", app_token="xapp-t")
    adapter.send = AsyncMock()

    # Use raw start to subscribe without initializing slack
    adapter._bus = bus
    adapter._running = True
    bus.subscribe_outbound(adapter.channel, adapter.send)

    msg = OutboundMessage(
        channel=Channel.SLACK,
        chat_id="C111",
        content="response",
    )
    await bus.publish_outbound(msg)

    adapter.send.assert_called_once_with(msg)
