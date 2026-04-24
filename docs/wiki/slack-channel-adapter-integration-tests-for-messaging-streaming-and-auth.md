---
{
  "title": "Slack Channel Adapter: Integration Tests for Messaging, Streaming, and Auth",
  "summary": "Tests for PocketPaw's Slack channel adapter, covering message send/receive, threaded replies, stream buffering, channel filtering, and token validation. Uses lightweight mock classes to avoid a real Slack dependency while exercising the full adapter surface.",
  "concepts": [
    "SlackAdapter",
    "BaseChannelAdapter",
    "MessageBus",
    "stream buffering",
    "channel filtering",
    "socket mode",
    "thread_ts",
    "token validation",
    "slack-bolt"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "messaging",
    "test"
  ],
  "source_docs": [
    "cc1a4cd6b5765a61"
  ],
  "backlinks": null,
  "word_count": 487,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw communicates with users through pluggable channel adapters that implement a common `BaseChannelAdapter` protocol. The Slack adapter is a production integration that uses the `slack-bolt` async framework under the hood. This test file validates every significant behaviour of that adapter without requiring live Slack credentials or network access.

## Mock Infrastructure

Three mock classes — `MockAsyncApp`, `MockAsyncSocketModeHandler`, and `MockAsyncWebClient` — stand in for `slack_bolt`'s async equivalents. A fourth, `MockSocketModeClient`, simulates error cases like failing auth and bad WebSocket URLs. This layered mocking approach lets tests run in CI without any external dependencies while still exercising the adapter's control flow, event routing, and error handling paths.

## Adapter Lifecycle

`test_start_stop` validates that the adapter cleanly starts the socket-mode handler and stops it. This matters because a leaked handler would leave a persistent WebSocket connection open, consuming quota and preventing clean process shutdown.

## Message Sending

Two send tests cover the happy paths:

- `test_send_normal_message`: A plain text message is dispatched without a `thread_ts`.
- `test_send_with_thread_ts`: When `thread_ts` is present in message metadata, the adapter threads the reply. This is critical for multi-turn conversations in shared channels — without threading, every response would break the conversation context for other users watching the channel.

## Stream Buffering

`test_stream_buffering` and `test_stream_flush_via_chat_update` verify the adapter's token-by-token streaming contract. Rather than sending a new Slack message for every token (which would spam the channel), the adapter accumulates tokens into a buffer and then updates a single placeholder message via `chat.update`. The flush test confirms that the final accumulated content is posted correctly when the stream ends.

## Channel Filtering

`test_channel_filtering` confirms that messages arriving from channels not in the allowed list are silently dropped. This prevents the agent from responding to channels it wasn't configured to monitor — an important boundary for enterprise deployments where the bot is invited broadly but should only respond in specific channels.

## Event Handlers

Three handler tests verify inbound event routing:

- `test_mention_handler`: `app_mention` events trigger agent processing.
- `test_dm_handler`: Direct messages (`channel_type=im`) are also processed.
- `test_thread_ts_in_metadata`: The `thread_ts` from Slack events is forwarded in the message metadata so the adapter can thread replies correctly.

## Token Validation

`test_invalid_bot_token_raises` and `test_invalid_app_token_raises` confirm that startup fails fast with a clear `RuntimeError` when either credential is invalid. Without this, the adapter would silently operate in a degraded state, dropping all messages with cryptic `auth_test` failures buried in logs.

## Bus Integration

`test_bus_integration` verifies the adapter's connection to PocketPaw's internal `MessageBus` — the pub/sub backbone that routes events between adapters, the agent loop, and tools.

## Known Gaps

The `MockSocketModeClient` has a `failing_wss` method stub that appears in the class definition but is not directly called in any exported test function visible in the AST, suggesting a test for WebSocket reconnection failures may be incomplete or deferred.

```python
# Adapter fixture pattern used across tests
@pytest.fixture
def adapter():
    return SlackAdapter(bot_token="xoxb-test", app_token="xapp-test")

@pytest.fixture
def bus():
    return MessageBus()
```
