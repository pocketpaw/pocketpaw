---
{
  "title": "Matrix Channel Adapter: Message Routing, Streaming, Error Recovery, and Bus Integration",
  "summary": "The Matrix adapter test suite (Sprint 21) validates PocketPaw's Matrix channel integration, covering initial-sync message filtering, room authorization, streaming with edit-in-place, MessageBus subscription lifecycle, and error isolation. The `matrix-nio` library is fully mocked as an optional dependency, allowing tests to run without a live Matrix homeserver.",
  "concepts": [
    "Matrix adapter",
    "matrix-nio",
    "initial sync filtering",
    "room authorization",
    "streaming",
    "edit-in-place",
    "MessageBus",
    "error isolation",
    "lifecycle management",
    "Channel.MATRIX",
    "self-message prevention"
  ],
  "categories": [
    "channel adapters",
    "messaging",
    "test"
  ],
  "source_docs": [
    "ffd70b7b19578975"
  ],
  "backlinks": null,
  "word_count": 529,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Matrix adapter connects PocketPaw to the Matrix protocol, enabling AI companion interactions in Matrix rooms. Because `matrix-nio` is an optional dependency (not all deployments need Matrix support), the test suite injects a mock `nio` module at the top level before importing the adapter. This pattern allows the adapter's logic to be tested in any environment.

## Initialization and Configuration

`TestMatrixAdapterInit` confirms default values — empty `homeserver` and `user_id`, `Channel.MATRIX` enum, device ID `"POCKETPAW"` — and verifies that custom config (homeserver URL, user ID, access token, allowed room IDs) is stored correctly. The device ID default matters for Matrix E2E encryption key management.

## Initial Sync Filtering

The `_initial_sync_done` flag is a critical guard. When a Matrix client first connects, the server replays the room's recent history. Without filtering, the bot would respond to messages that were already processed in a previous session, causing duplicate replies. `test_initial_sync_messages_skipped` and `test_initial_sync_media_messages_skipped` verify that both text and media events are dropped when `_initial_sync_done` is `False`.

## Message Authorization

`test_unauthorized_room_filtered` verifies that messages from rooms not in `allowed_room_ids` are silently dropped. This prevents the bot from responding in rooms it was added to without explicit authorization — important for shared homeservers where the bot account might be invited to arbitrary rooms.

## Self-Message Echo Prevention

`test_skip_own_messages` confirms that when the event's `sender` matches the bot's `user_id`, the message is not re-published to the bus. Without this guard, a bot that sends a message would trigger its own message handler, creating an infinite loop.

## Streaming with Edit-in-Place

Matrix supports message edits via the `m.replace` relation. The streaming tests verify that:

- Incremental stream chunks accumulate in a buffer.
- `stream_end` flushes the buffer and sends the complete message.
- When an `event_id` is available from the initial send, subsequent stream chunks are sent as edits rather than new messages, giving a "typing" effect.
- After `stream_end`, the edit state (`_current_edit_event_id`) is cleared to prevent stale edit references on the next stream.

## Error Isolation

`TestMatrixAdapterErrorRecovery` validates that exceptions in `_on_message`, `_on_media_message`, and `send()` are caught and logged rather than propagating. If an exception escapes the callback, `matrix-nio` would terminate the sync loop, taking the entire adapter offline. The tests confirm that send failures return an error `OutboundMessage` rather than raising.

## MessageBus Integration

Tests in `TestMatrixAdapterBusIntegration` verify that:

- On `start()`, the adapter subscribes to outbound messages on the bus for the Matrix channel.
- Inbound messages from Matrix rooms are published to the bus as `InboundMessage` events.
- On `stop()`, the bus subscription is cancelled to prevent memory leaks and ghost deliveries.

## Lifecycle Safety

`test_double_stop_is_safe` confirms that calling `stop()` twice does not raise. This is important for cleanup code that may call `stop()` in both a normal teardown and an exception handler.

`test_start_without_homeserver_logs_error` verifies that misconfigured adapters (no homeserver set) log an error and return rather than attempting a connection that would hang or crash.

## Known Gaps

- Rate limiting in `TestMatrixAdapterStreaming` is tested at the mock level but does not simulate actual Matrix server-side rate limit responses (HTTP 429).
- E2E encryption key verification flows are not tested — the adapter operates in unencrypted mode in tests.