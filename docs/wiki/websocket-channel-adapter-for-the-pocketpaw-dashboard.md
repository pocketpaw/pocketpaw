---
{
  "title": "WebSocket Channel Adapter for the PocketPaw Dashboard",
  "summary": "WebSocketAdapter manages real-time bidirectional communication between PocketPaw's FastAPI backend and its web dashboard frontend, routing messages, streaming chunks, and system events (tool execution, thinking indicators) to the correct connected browser session.",
  "concepts": [
    "WebSocket",
    "FastAPI WebSocket",
    "system events",
    "session_key routing",
    "stream_start",
    "stream_end",
    "base64 media upload",
    "broadcast",
    "connection registry",
    "BaseChannelAdapter",
    "dashboard UI"
  ],
  "categories": [
    "channel-adapters",
    "websocket",
    "dashboard",
    "streaming"
  ],
  "source_docs": [
    "a70c9504d7348fb5"
  ],
  "backlinks": null,
  "word_count": 493,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

WebSocketAdapter is the primary channel for PocketPaw's own web dashboard. Unlike external platform adapters, it communicates directly with browser clients over FastAPI WebSocket connections. Each open browser tab registers a connection under its `chat_id`, and the adapter routes outbound messages and internal system events to exactly the matching connection.

## Connection Registry

Active connections are stored in `_connections: dict[str, WebSocket]`. Keys are `chat_id` strings; values are FastAPI `WebSocket` objects. `register_connection()` and `unregister_connection()` are called by the route handler, not the adapter itself — the adapter assumes the WebSocket handshake (`websocket.accept()`) is handled upstream. This separation keeps auth and upgrade logic in the HTTP layer.

If no connection matches a given `chat_id` in `send()`, the message is silently dropped. The comment notes this is intentional — the message was either handled by the SSE bridge or the client disconnected.

## System Event Routing

WebSocketAdapter subscribes to `SystemEvent` messages via `bus.subscribe_system()` in its `start()` override. System events carry internal signals like `tool_start`, `tool_end`, `agent_start`, and `agent_end` that the frontend uses to render thinking indicators and tool execution progress.

Each system event contains `session_key` in the format `"websocket:<chat_id>"`. The adapter splits on `:` to extract the `chat_id` and routes the event only to the matching WebSocket — global daemon events without a `session_key` are dropped entirely, since the frontend fetches those via REST.

A 2026-02-05 fix changed the system event payload from a nested structure to a flat `{"type": "system_event", "event_type": ..., "data": {...}}` format because the original nested shape was not parseable by the frontend renderer.

## Message Protocol

The wire format for different message types:

- **Stream chunk**: `{"type": "message", "content": "...", "is_stream_chunk": true, "metadata": {...}}`
- **Stream end**: `{"type": "stream_end", "media": [...]}`  
- **Stream start**: `{"type": "stream_start"}` — sent before the inbound message is published to initialize the UI
- **System event**: `{"type": "system_event", "event_type": "...", "data": {...}}`
- **Broadcast**: `{"type": "notification", "content": ...}` (or custom type)

## Inbound Media Handling

The `handle_message()` method processes `action: "chat"` messages. Attached media items arrive as base64-encoded blobs in the `media` array: `[{"data": "<base64>", "name": "file.png", "mime_type": "image/png"}]`. Each blob is decoded and saved to disk via `MediaDownloader.save_from_bytes()`, and a `[Attached: name]` hint is appended to the text content.

A commented-out `save_user_message` call and its explanation note that saving was moved to `MongoMemoryStore` via `memory.add_to_session` in the agent loop. The inline call had been producing duplicate database rows — one with and one without attachments.

## Broadcast

`broadcast()` fans out a message to all currently connected clients. This is used for global notifications (e.g., server restart warnings, deployment notices) that are not scoped to a single session.

## Known Gaps

- There is no reconnection handling at the adapter level; if a client disconnects and reconnects with the same `chat_id`, the old WebSocket object is overwritten without cleanup of the old connection.
- No backpressure mechanism exists for slow clients — `send_json()` calls can pile up if the browser tab is backgrounded or unresponsive.