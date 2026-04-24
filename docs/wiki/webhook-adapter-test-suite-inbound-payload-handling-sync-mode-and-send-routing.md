---
{
  "title": "Webhook Adapter Test Suite: Inbound Payload Handling, Sync Mode, and Send Routing",
  "summary": "This module tests the `WebhookAdapter`, PocketPaw's generic inbound HTTP webhook channel. It covers slot configuration, payload normalization, sync vs. async dispatch modes, future-based response resolution, and streaming accumulation.",
  "concepts": [
    "WebhookAdapter",
    "WebhookSlotConfig",
    "InboundMessage",
    "Channel.WEBHOOK",
    "sync mode",
    "async mode",
    "asyncio.Future",
    "streaming",
    "_pending",
    "_buffers",
    "payload normalization",
    "message bus"
  ],
  "categories": [
    "testing",
    "webhook",
    "channel adapters",
    "message bus",
    "test"
  ],
  "source_docs": [
    "abeb0fcccb826b66"
  ],
  "backlinks": null,
  "word_count": 525,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_webhook_adapter.py` validates `pocketpaw.bus.adapters.webhook_adapter.WebhookAdapter`, the channel that accepts arbitrary inbound HTTP webhooks and routes them through the message bus. The tests were created on 2026-02-09 and exercise three behavioural layers: slot configuration, inbound payload handling, and outbound response routing.

## Slot Configuration

`WebhookSlotConfig` is the per-webhook configuration object. `TestWebhookSlotConfig` verifies:

- **Defaults**: `description=""` and `sync_timeout=30` when not specified. These prevent `AttributeError` at runtime on partially-configured slots.
- **Custom values**: All four fields (`name`, `secret`, `description`, `sync_timeout`) round-trip correctly.

## Adapter Properties

`TestWebhookAdapterProperties` asserts:

- `adapter.channel == Channel.WEBHOOK` — so the bus can route messages to the correct adapter.
- `adapter._pending == {}` and `adapter._buffers == {}` on construction — these dicts track in-flight sync requests; they must start empty to avoid state bleed between restarts.

## Inbound Payload Handling (`TestHandleWebhookAsync`)

`handle_webhook` is called when an HTTP POST arrives. Five tests cover it:

- **Standard payload**: A body with `{"content": "hello", "sender": "user@github"}` should produce an `InboundMessage` with `content`, `sender_id`, `chat_id`, and `channel` correctly mapped.
- **Raw fallback**: If the body has no `content` key, the entire body dict becomes the content. This accommodates third-party webhooks (e.g., GitHub, Stripe) that do not follow PocketPaw's envelope format.
- **Default sender**: If `sender` is absent, the adapter substitutes a sensible default rather than raising `KeyError`.
- **Metadata merge**: Extra fields in the body should be merged into the message's metadata dict.
- **Non-dict metadata ignored**: If the body's metadata field is not a dict (e.g., a string), it is silently dropped rather than causing a type error downstream.

## Sync Mode (`TestHandleWebhookSync`)

Sync mode allows the HTTP caller to wait for the agent's reply before the response is returned. It works via `asyncio.Future`:

- **Resolves with response**: When `send()` delivers a non-streaming `OutboundMessage`, the future is resolved and `handle_webhook` returns the message content. This test uses an inner `respond()` coroutine to simulate the agent replying.
- **Timeout**: If no reply arrives within `slot.sync_timeout` seconds, `handle_webhook` must return a timeout error string rather than hanging forever. The test uses a very short timeout (configured on the fixture) to keep the suite fast.
- **Stream accumulation**: If the agent streams chunks, they accumulate in `_buffers` and are joined before resolving the future. This allows streaming agents to work in sync mode without the caller receiving a partial response.

## Send Routing (`TestSendMethod`)

`send()` is called by the bus when the agent produces an outbound message on the webhook channel:

- **No waiter**: If no future is pending for a given `chat_id`, `send()` logs and returns without error. This prevents `KeyError` when the caller has already timed out or disconnected.
- **Resolves future**: If a future is pending, `send()` resolves it with the message content.
- **Stream chunks accumulate**: Streaming chunks are buffered until the stream is marked complete, then the accumulated result resolves the future.

## Known Gaps

- No test covers concurrent requests to the same slot (two simultaneous POST calls with the same `chat_id`), so the `_pending` dict's behavior under race conditions is untested.
- The secret-validation logic (HMAC verification) is tested in `test_webhook_routes.py`, not here—so this file has no coverage of malformed or missing `X-Webhook-Secret` headers.
