# Tests for WhatsApp Channel Adapter
# Created: 2026-02-06

from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketclaw.bus.adapters.whatsapp_adapter import WhatsAppAdapter
from pocketclaw.bus.events import Channel, OutboundMessage
from pocketclaw.bus.queue import MessageBus


@pytest.fixture
def adapter():
    return WhatsAppAdapter(
        access_token="test-token",
        phone_number_id="123456789",
        verify_token="verify-secret",
        allowed_phone_numbers=["+1234567890"],
    )


@pytest.fixture
def bus():
    return MessageBus()


def test_channel_property(adapter):
    assert adapter.channel == Channel.WHATSAPP


async def test_start_stop(adapter, bus):
    await adapter.start(bus)
    assert adapter._running is True
    assert adapter._http is not None

    await adapter.stop()
    assert adapter._running is False


def test_webhook_verify_success(adapter):
    """Successful webhook verification returns challenge."""
    result = adapter.handle_webhook_verify("subscribe", "verify-secret", "challenge-123")
    assert result == "challenge-123"


def test_webhook_verify_wrong_token(adapter):
    """Wrong token returns None."""
    result = adapter.handle_webhook_verify("subscribe", "wrong-token", "challenge-123")
    assert result is None


def test_webhook_verify_wrong_mode(adapter):
    """Wrong mode returns None."""
    result = adapter.handle_webhook_verify("unsubscribe", "verify-secret", "challenge-123")
    assert result is None


async def test_send_text_message(adapter, bus):
    await adapter.start(bus)

    mock_response = MagicMock()
    mock_response.status_code = 200
    adapter._http.post = AsyncMock(return_value=mock_response)

    msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="+1234567890",
        content="Hello WhatsApp!",
    )
    await adapter.send(msg)

    adapter._http.post.assert_called_once()
    call_args = adapter._http.post.call_args
    assert call_args[1]["json"]["to"] == "+1234567890"
    assert call_args[1]["json"]["text"]["body"] == "Hello WhatsApp!"
    assert call_args[1]["json"]["messaging_product"] == "whatsapp"

    await adapter.stop()


async def test_stream_accumulation(adapter, bus):
    """WhatsApp doesn't stream — chunks accumulate and send on stream_end."""
    await adapter.start(bus)

    mock_response = MagicMock()
    mock_response.status_code = 200
    adapter._http.post = AsyncMock(return_value=mock_response)

    # Send chunks
    chunk1 = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="+1234567890",
        content="Hello ",
        is_stream_chunk=True,
    )
    chunk2 = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="+1234567890",
        content="World!",
        is_stream_chunk=True,
    )
    await adapter.send(chunk1)
    await adapter.send(chunk2)

    # Nothing sent yet
    adapter._http.post.assert_not_called()
    assert adapter._buffers["+1234567890"] == "Hello World!"

    # Stream end triggers send
    end_msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="+1234567890",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end_msg)

    adapter._http.post.assert_called_once()
    call_args = adapter._http.post.call_args
    assert call_args[1]["json"]["text"]["body"] == "Hello World!"
    assert "+1234567890" not in adapter._buffers

    await adapter.stop()


async def test_phone_number_filtering(adapter, bus):
    """Messages from non-allowed numbers are ignored."""
    await adapter.start(bus)
    adapter._http.post = AsyncMock(return_value=MagicMock(status_code=200))

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+9999999999",  # not allowed
                                    "type": "text",
                                    "text": {"body": "hello"},
                                    "id": "msg1",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    await adapter.handle_webhook_message(payload)
    assert bus.inbound_pending() == 0

    await adapter.stop()


async def test_text_webhook_parsing(adapter, bus):
    """Text messages are parsed and published to bus."""
    await adapter.start(bus)
    adapter._http.post = AsyncMock(return_value=MagicMock(status_code=200))

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+1234567890",
                                    "type": "text",
                                    "text": {"body": "Hello from WhatsApp"},
                                    "id": "msg1",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    await adapter.handle_webhook_message(payload)
    assert bus.inbound_pending() == 1

    msg = await bus.consume_inbound()
    assert msg.content == "Hello from WhatsApp"
    assert msg.channel == Channel.WHATSAPP
    assert msg.sender_id == "+1234567890"
    assert msg.chat_id == "+1234567890"

    await adapter.stop()


async def test_image_webhook_parsing(adapter, bus):
    """Image messages extract caption or placeholder."""
    await adapter.start(bus)
    adapter._http.post = AsyncMock(return_value=MagicMock(status_code=200))

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+1234567890",
                                    "type": "image",
                                    "image": {"caption": "Look at this!"},
                                    "id": "msg2",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    await adapter.handle_webhook_message(payload)
    msg = await bus.consume_inbound()
    # Caption is present, so content starts with it
    assert "Look at this!" in msg.content

    await adapter.stop()


async def test_read_receipts(adapter, bus):
    """Read receipts are sent after processing."""
    await adapter.start(bus)
    mock_response = MagicMock(status_code=200)
    adapter._http.post = AsyncMock(return_value=mock_response)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+1234567890",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                    "id": "wamid.123",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    await adapter.handle_webhook_message(payload)

    # Should have called post twice: once for read receipt
    # The read receipt call
    calls = adapter._http.post.call_args_list
    assert len(calls) == 1  # Just the read receipt
    read_call = calls[0]
    assert read_call[1]["json"]["status"] == "read"
    assert read_call[1]["json"]["message_id"] == "wamid.123"

    await adapter.stop()


async def test_bus_integration(bus):
    """Adapter receives outbound messages from bus subscription."""
    adapter = WhatsAppAdapter(access_token="t", phone_number_id="p", verify_token="v")
    adapter.send = AsyncMock()

    adapter._bus = bus
    adapter._running = True
    bus.subscribe_outbound(adapter.channel, adapter.send)

    msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="+1234567890",
        content="response",
    )
    await bus.publish_outbound(msg)

    adapter.send.assert_called_once_with(msg)


async def test_extract_content_and_media_types():
    """Various message types extract content correctly."""
    adapter = WhatsAppAdapter(access_token="t", phone_number_id="p", verify_token="v")

    content, media = await adapter._extract_content_and_media(
        {"type": "text", "text": {"body": "hi"}}
    )
    assert content == "hi"
    assert media == []

    content, media = await adapter._extract_content_and_media({"type": "image", "image": {}})
    assert "image" in content.lower() or "received" in content.lower()

    content, media = await adapter._extract_content_and_media(
        {"type": "image", "image": {"caption": "pic"}}
    )
    assert "pic" in content

    content, media = await adapter._extract_content_and_media({"type": "audio", "audio": {}})
    assert "audio" in content.lower() or "received" in content.lower()

    content, media = await adapter._extract_content_and_media({"type": "sticker", "sticker": {}})
    assert "sticker" in content.lower() or "received" in content.lower()


async def test_no_allowed_numbers_means_all_allowed(bus):
    """No allowed_phone_numbers means all numbers are allowed."""
    adapter = WhatsAppAdapter(
        access_token="t",
        phone_number_id="p",
        verify_token="v",
        allowed_phone_numbers=[],
    )
    await adapter.start(bus)
    adapter._http.post = AsyncMock(return_value=MagicMock(status_code=200))

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "+9999999999",
                                    "type": "text",
                                    "text": {"body": "anyone"},
                                    "id": "msg1",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    await adapter.handle_webhook_message(payload)
    assert bus.inbound_pending() == 1

    await adapter.stop()
