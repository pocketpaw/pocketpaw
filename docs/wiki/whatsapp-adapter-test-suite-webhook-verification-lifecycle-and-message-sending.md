---
{
  "title": "WhatsApp Adapter Test Suite: Webhook Verification, Lifecycle, and Message Sending",
  "summary": "This module tests PocketPaw's `WhatsAppAdapter`, covering the Meta webhook verification handshake, adapter lifecycle (`start`/`stop`), outbound message delivery, allowed-number filtering, inbound message routing, and media handling. It ensures the adapter correctly implements the WhatsApp Cloud API protocol.",
  "concepts": [
    "WhatsAppAdapter",
    "webhook verification",
    "Meta WhatsApp Cloud API",
    "allowed_phone_numbers",
    "MessageBus",
    "OutboundMessage",
    "InboundMessage",
    "Channel.WHATSAPP",
    "phone_number_id",
    "verify_token",
    "adapter lifecycle"
  ],
  "categories": [
    "testing",
    "WhatsApp",
    "channel adapters",
    "message bus",
    "test"
  ],
  "source_docs": [
    "0dc03a7f3134155c"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_whatsapp_adapter.py` validates `pocketpaw.bus.adapters.whatsapp_adapter.WhatsAppAdapter`. The adapter bridges PocketPaw's internal message bus with the Meta WhatsApp Business Cloud API, which requires a specific webhook verification handshake and uses different HTTP methods and payload shapes from other channels.

## Fixture Design

The `adapter` fixture constructs a `WhatsAppAdapter` with explicit credentials: `access_token`, `phone_number_id`, `verify_token`, and an `allowed_phone_numbers` list. The `bus` fixture creates a real `MessageBus`. This combination tests the adapter in a realistic configuration without any mocking at the adapter level—HTTP calls are patched at the `_http` layer.

## Adapter Lifecycle

`test_start_stop` verifies the adapter correctly manages its state:

- After `start(bus)`: `adapter._running is True` and `adapter._http is not None` (the HTTP client has been initialized).
- After `stop()`: `adapter._running is False`.

These guards prevent the adapter from processing messages after it has been stopped, which could cause double-delivery or send attempts against a closed HTTP client.

## Webhook Verification Handshake

Meta's WhatsApp API requires the bot server to respond to a `GET` challenge with the challenge value to prove ownership. `handle_webhook_verify` implements this:

- `test_webhook_verify_success`: Correct mode (`"subscribe"`) and correct token returns the challenge string.
- `test_webhook_verify_wrong_token`: Wrong token returns `None`, causing the route to respond with 403 and failing the Meta verification.
- `test_webhook_verify_wrong_mode`: A mode other than `"subscribe"` returns `None`. This prevents accidental acceptance of unsupported webhook event types.

Without these guards, a misconfigured or malicious request could complete the verification handshake and start delivering events to the wrong server.

## Outbound Message Delivery

`test_send_text_message` confirms that `adapter.send(msg)` translates an `OutboundMessage` into the correct WhatsApp Cloud API call:

- `adapter._http.post` is called exactly once.
- The POST body's `to` field equals the `chat_id` from the message.
- The message type and text content are formatted per the WhatsApp Cloud API schema.

The test patches `adapter._http.post` as an `AsyncMock` returning a 200 response, avoiding real API calls.

## Allowed-Number Filtering

`WhatsAppAdapter` is constructed with `allowed_phone_numbers=["+1234567890"]`. Inbound messages from numbers not on this list should be silently dropped, preventing the bot from responding to arbitrary callers. This guard exists because WhatsApp numbers are not secret—anyone can message the bot if no filtering is applied.

## Inbound Message Routing

Inbound webhook POSTs from Meta contain a nested payload with sender phone number, message text, and message ID. Tests verify that the adapter parses this structure and publishes an `InboundMessage` to the bus with the correct `sender_id`, `content`, and `channel`.

## Known Gaps

- No test covers the adapter's behavior when the WhatsApp API returns a non-200 status on `send()`—whether it raises, logs, or silently swallows the error is untested.
- Media message types (image, audio, document) have limited test coverage compared to text messages.
- No test verifies deduplication of inbound messages—WhatsApp can deliver the same webhook event multiple times on retries.
