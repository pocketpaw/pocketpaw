---
{
  "title": "Google Chat Channel Adapter Tests",
  "summary": "This test module provides full coverage for `GoogleChatAdapter`, which connects PocketPaw to Google Chat via webhook or Pub/Sub mode. It covers initialization, webhook message handling (including slash commands and space filtering), streaming message buffering, error recovery, `MessageBus` integration, and adapter lifecycle management.",
  "concepts": [
    "GoogleChatAdapter",
    "Google Chat",
    "webhook",
    "Pub/Sub",
    "MessageBus",
    "streaming buffer",
    "slash commands",
    "argumentText",
    "space filter",
    "error recovery",
    "adapter lifecycle",
    "optional dependencies"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "Google integrations",
    "test"
  ],
  "source_docs": [
    "a045ba271a21d4b4"
  ],
  "backlinks": null,
  "word_count": 516,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Dependency Mocking Strategy

The `google-api-python-client` library is an optional dependency. The file mocks it at the `sys.modules` level before importing the adapter:

```python
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", mock_oauth2)
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", mock_discovery)
```

This pattern allows the adapter to be tested in environments where Google libraries are not installed — critical for lightweight CI runners and for users who do not need Google Chat support.

## Initialization Tests (TestGoogleChatAdapterInit)

Two tests verify defaults (`mode="webhook"`, `channel=Channel.GOOGLE_CHAT`, empty `allowed_space_ids`) and a fully configured instance (`mode="pubsub"`, with `project_id`, `subscription_id`, and `allowed_space_ids`).

## Webhook Message Handling (TestGoogleChatAdapterWebhook)

Five tests cover the inbound path:

- **Valid message** — a standard `MESSAGE` event produces an `InboundMessage` published to the bus with correct `content`, `sender_id`, `chat_id`, `channel`, and `sender_display_name` metadata.
- **Non-message events** — `ADDED_TO_SPACE` and similar lifecycle events are silently ignored.
- **Empty text skipped** — messages with `text: ""` do not produce bus publishes unless `argumentText` is set.
- **Space filter** — when `allowed_space_ids` is configured, messages from unlisted spaces are dropped before reaching the bus.
- **Slash command fallback** — when `text` is empty but `argumentText` is set (Google Chat's representation of slash commands), the adapter uses `argumentText` as the message content.

The `argumentText` fallback is subtle: Google Chat sends slash command arguments in a separate field, and an adapter that only reads `text` would silently drop all slash command interactions.

## Send Tests (TestGoogleChatAdapterSend)

- **Normal message** — a non-streaming `OutboundMessage` triggers a `spaces().messages().create()` API call.
- **Stream accumulation** — `is_stream_chunk=True` messages are buffered by `chat_id` (`adapter._buffers`).
- **Stream flush** — `is_stream_end=True` sends the accumulated buffer and clears it.
- **Empty message skipped** — whitespace-only content is dropped without an API call.
- **No service** — when `_chat_service` is `None` (adapter not yet authenticated), `send()` returns silently rather than raising.

## Error Recovery (TestGoogleChatAdapterErrorRecovery)

All three error cases verify that exceptions do not propagate out of the adapter:

- API error during send is caught in `_send_text()`.
- `RuntimeError` from the bus publish is caught in `handle_webhook_message()`.
- Missing `message` key in the webhook payload is handled gracefully.

This defensive approach is necessary because Google Chat may send malformed payloads during API upgrades or due to third-party webhook proxies.

## Bus Integration (TestGoogleChatAdapterBusIntegration)

Three integration tests use a real `MessageBus` instance:

- Outbound messages published to the bus reach the adapter's `send` method.
- Inbound messages published via `_publish_inbound` appear in the bus queue.
- After `stop()`, the adapter unsubscribes so further outbound messages do not reach it.

## Lifecycle Tests (TestGoogleChatAdapterLifecycle)

- `start()` sets `_running = True`; `stop()` clears it.
- Double `stop()` is safe (idempotent).
- In `pubsub` mode with full config, `start()` creates a poll task.
- `stop()` cancels the poll task, which is verified by checking `task.done() or task.cancelled()`.

## Pub/Sub Mode (TestGoogleChatAdapterPubSub)

- Webhook mode never creates a poll task (`_poll_task is None`).
- Pub/Sub mode without `project_id`/`subscription_id` does not start polling.

## Known Gaps

No tests cover the actual Pub/Sub message decoding from a Google Cloud Pub/Sub pull response. There are no tests for `_init_credentials` with a real service account key file format.