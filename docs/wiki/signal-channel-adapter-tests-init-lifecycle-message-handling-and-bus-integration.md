---
{
  "title": "Signal Channel Adapter Tests — Init, Lifecycle, Message Handling, and Bus Integration",
  "summary": "This Sprint 20 test file validates `SignalAdapter`, PocketPaw's channel adapter for Signal messenger. Tests cover adapter configuration defaults, start/stop lifecycle, inbound message parsing with phone number authorization, outbound message sending with streaming support, error recovery, and MessageBus integration.",
  "concepts": [
    "SignalAdapter",
    "Signal messenger",
    "channel adapter",
    "MessageBus",
    "phone number authorization",
    "inbound message",
    "outbound message",
    "streaming chunks",
    "signal-cli",
    "httpx",
    "start/stop lifecycle",
    "error recovery"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "messaging",
    "integrations",
    "test"
  ],
  "source_docs": [
    "80de83fd95580998"
  ],
  "backlinks": null,
  "word_count": 520,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_signal_adapter.py` tests `SignalAdapter` from `pocketpaw.bus.adapters.signal_adapter`. Signal is an end-to-end encrypted messaging platform; this adapter lets PocketPaw agents receive and send messages via a local `signal-cli` REST API bridge (default: `http://localhost:8080`). All HTTP interactions are mocked.

## Initialization Defaults and Custom Config

`TestSignalAdapterInit`:

- Default adapter has `api_url == "http://localhost:8080"`, empty `phone_number`, empty `allowed_phone_numbers`, and `channel == Channel.SIGNAL`.
- Custom config with explicit URL, phone number, and allowlist is stored correctly.
- Trailing slashes on `api_url` are stripped (`"http://signal:9090/"` → `"http://signal:9090"`), preventing double-slash URL construction when appending paths.

## Start/Stop Lifecycle

`TestSignalAdapterStartStop`:

- `test_start_sets_running` — after `start(bus)`, `adapter._running is True` and `adapter._http` is not None (HTTP client is created). After `stop()`, `_running` is False.
- `test_start_without_phone_number` — the adapter starts without crashing even if `phone_number` is empty. It logs an error (verified in `TestSignalAdapterLifecycle`) but does not raise. This allows the adapter to be instantiated before the phone number is configured.

## Inbound Message Handling

`TestSignalAdapterHandleMessage` tests `_handle_message(msg_data)`:

- **Valid message** — parses `envelope.source` as sender, `dataMessage.message` as content, and publishes to the bus via `publish_inbound`.
- **No content** — `dataMessage.message` is None or missing; message is silently dropped (no crash, no publish).
- **No source** — `envelope.source` is missing; message is dropped.
- **Unauthorized** — sender is not in `allowed_phone_numbers` (when the allowlist is non-empty); message is dropped. This is the authorization gate preventing unknown callers from interacting with the agent.
- **`sourceNumber` fallback** — some signal-cli versions use `sourceNumber` instead of `source`; the adapter handles both.

## Outbound Message Sending

`TestSignalAdapterSend` tests `send(message)`:

- **Normal message** — HTTP POST is called with the message content.
- **Stream chunks** — `send` is called with streaming `OutboundMessage` chunks; each chunk is posted individually.
- **Empty message skipped** — an empty content string is not posted (prevents sending blank Signal messages).
- **Without HTTP client** — calling `send` before `start` does not crash; logs a warning.

## Error Recovery

`TestSignalAdapterErrorRecovery` — all error scenarios result in logging rather than exceptions:

- API error (non-200 response) — logged.
- Auth error (401/403) — logged.
- Rate limit error (429) — logged.
- Network exception (`httpx.RequestError`) — caught and logged.
- Exception in `_handle_message` — caught and logged, preventing a single malformed message from crashing the polling loop.

```python
async def test_send_network_exception_caught():
    # httpx.RequestError during send is caught; no exception propagates
```

## MessageBus Integration

`TestSignalAdapterBusIntegration`:

- `test_bus_outbound_subscription` — on `start`, the adapter subscribes to outbound messages for `Channel.SIGNAL`.
- `test_inbound_message_published` — when `_handle_message` processes a valid message, the bus's `publish_inbound` is called.
- `test_stop_unsubscribes_from_bus` — on `stop`, the adapter unsubscribes, preventing leaked subscriptions.

## Lifecycle Edge Cases

- `test_start_creates_http_client_and_poll_task` — `_http` and `_poll_task` are set after start.
- `test_stop_cancels_poll_task` — the poll task is cancelled on stop.
- `test_stop_closes_http_client` — the HTTP client is closed on stop, freeing connection pool resources.
- `test_double_stop_is_safe` — calling `stop` twice does not crash.
- `test_start_without_phone_logs_error` — missing phone number is logged at error level.

## Known Gaps

No `TODO` or `FIXME` markers. Tests do not cover reconnection behavior (what happens when the signal-cli bridge goes down mid-session) or message ordering guarantees during high-throughput streaming.
