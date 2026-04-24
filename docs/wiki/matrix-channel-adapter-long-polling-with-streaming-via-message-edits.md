---
{
  "title": "Matrix Channel Adapter — Long-Polling with Streaming via Message Edits",
  "summary": "MatrixAdapter connects PocketPaw to the Matrix protocol using matrix-nio's AsyncClient, supporting both access token and password authentication. Streaming agent responses are delivered by editing the initial message in place using Matrix's m.replace relation, rate-limited to one edit per 1.5 seconds to avoid homeserver rate limiting.",
  "concepts": [
    "MatrixAdapter",
    "matrix-nio",
    "AsyncClient",
    "sync_forever",
    "m.replace",
    "streaming edits",
    "rate limiting",
    "access token",
    "password auth",
    "initial sync skip",
    "allowed_room_ids",
    "media messages"
  ],
  "categories": [
    "channel-adapters",
    "matrix",
    "message-bus",
    "streaming"
  ],
  "source_docs": [
    "0000000000000014"
  ],
  "backlinks": null,
  "word_count": 498,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Matrix Protocol Integration

Matrix is a federated, open-source messaging protocol. Unlike Telegram or Discord, there is no official Python SDK maintained by the platform — `matrix-nio` is the de facto community library. `MatrixAdapter` uses nio's `AsyncClient.sync_forever()` for long-polling: the client sends a sync request to the homeserver and holds the connection open until new events arrive, then processes them and immediately sends another sync request.

## Authentication Paths

The adapter supports two authentication methods:

- **Access token** — The token is set directly on the `AsyncClient` without an API call. This is the preferred method for production: tokens are long-lived, and skipping the login call saves one round-trip on startup.
- **Password** — Calls `AsyncClient.login()` at startup and logs a warning if login fails. Used for development or in environments where generating access tokens in advance is impractical.

Both paths require `homeserver` and `user_id` to be set. If either is missing, `_on_start()` logs an error and returns without raising, following the same defensive pattern as `GoogleChatAdapter`.

## Initial Sync Skip

Matrix's sync protocol delivers all events since the last sync token, including events that arrived while the client was offline. On first connect, this would cause the bot to process every historical message in every room — potentially hundreds of messages. The adapter uses an `_initial_sync_done` flag: events received before the first sync completes are ignored. Only messages that arrive *after* the initial sync are processed.

## Media Message Handling

Beyond text (`RoomMessageText`), the adapter registers callbacks for image, file, audio, and video events (`RoomMessageImage`, `RoomMessageFile`, `RoomMessageAudio`, `RoomMessageVideo`). These are published to the bus as `InboundMessage` with a content type annotation so the agent can handle file uploads differently from text messages. The media callback registration is wrapped in a try/except for `ImportError` since older versions of matrix-nio may not export all these event types.

## Streaming via m.replace

Matrix does not have a native streaming API. To simulate token-by-token streaming (as seen in Telegram or Discord), the adapter:

1. Sends an initial message with the first chunk of the response.
2. Stores the `event_id` of that message in `_edit_event_ids`.
3. For subsequent chunks, sends a replacement event (`m.relates_to.rel_type = m.replace`) that replaces the original message with the accumulated text.

The `_EDIT_RATE_LIMIT = 1.5` seconds minimum interval between edits prevents homeserver rate limiting. The timestamp of the last edit for each room is tracked in `_last_edit_time`. If a new chunk arrives before 1.5 seconds have elapsed, the adapter buffers it in `_buffers` and flushes on the next allowed edit slot.

## Room Access Control

`allowed_room_ids` restricts the adapter to a whitelist of Matrix room IDs. An empty list permits all rooms. The check happens inside `_on_message()` before the event is placed on the bus.

## Known Gaps

The streaming buffer does not have a maximum size. For very long agent responses on a slow homeserver, the buffer could grow unbounded if the rate limiter keeps deferring flushes. A maximum buffer size with forced-flush logic would prevent this.