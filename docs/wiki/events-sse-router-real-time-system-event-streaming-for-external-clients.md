---
{
  "title": "Events SSE Router — Real-Time System Event Streaming for External Clients",
  "summary": "The events router provides a Server-Sent Events endpoint that external clients (such as the Tauri desktop app) can subscribe to for real-time system events without connecting to the WebSocket. It delivers the same event types as the WebSocket — tool_start, tool_result, thinking, error, health_update, inbox_update — via a keepalive-aware HTTP stream.",
  "concepts": [
    "SSE",
    "Server-Sent Events",
    "SystemEvent",
    "message bus",
    "keepalive",
    "real-time",
    "WebSocket alternative",
    "event stream",
    "Tauri",
    "tool_start",
    "health_update",
    "inbox_update"
  ],
  "categories": [
    "API",
    "Real-time",
    "Events"
  ],
  "source_docs": [
    "d9e39be68b71560d"
  ],
  "backlinks": null,
  "word_count": 395,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's primary real-time channel is a WebSocket connection. However, not all clients can maintain a persistent WebSocket — HTTP-only environments, server-side consumers, and some desktop app embeddings work better with Server-Sent Events (SSE), which are unidirectional and ride on a standard HTTP connection. `events.py` provides this alternative transport.

## Architecture: Queue-Mediated Bus Subscription

The event generator follows a producer-consumer pattern:

1. A new `asyncio.Queue` is created per connection.
2. `_subscribe()` registers a callback on the message bus that pushes `SystemEvent` objects into the queue.
3. `_event_generator()` drains the queue and yields SSE-formatted strings.
4. On disconnect (when the generator's `finally` block runs), the bus subscription is removed.

This design is intentionally identical to the pattern used in `chat.py`'s `_APISessionBridge`, making the codebase consistent and the mental model transferable.

## SSE Envelope Shape

The 2026-02-25 update aligned the SSE envelope with `chat.py` by standardizing on the `"event"` key:

```
event: tool_start
data: {"event": "tool_start", "data": {...}}
```

Before this fix, `events.py` used `"event_type"` while `chat.py` used `"event"`, forcing clients to handle two different shapes for what are semantically the same events.

## Keepalive to Prevent Proxy Timeouts

The event generator sends a 30-second keepalive comment:

```python
yield ": keepalive\n\n"
```

SSE comments (lines starting with `:`) are ignored by SSE parsers but transmitted over the wire. Without this, reverse proxies with short idle-connection timeouts (nginx's default is 60s) will close the connection and the client will receive an unexpected disconnect. The comment keeps the TCP connection alive without sending a meaningful event.

## Initial Heartbeat

On connection, the generator immediately yields a `connected` event:

```python
yield f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
```

This serves two purposes: it confirms to the client that the subscription was established successfully, and it flushes any HTTP buffers that might otherwise hold the response until more data arrives.

## Event Types Delivered

The same event taxonomy as the WebSocket: `tool_start`, `tool_result`, `thinking`, `error`, `health_update`, `inbox_update`, and any other `SystemEvent` published on the bus. The router is event-type agnostic — new event types added to the bus flow through automatically.

## Known Gaps

There is no authentication on this endpoint in the visible source. If `GET /events/stream` is accessible without a valid session token or API key, an unauthenticated client could observe all system events including tool outputs and inbox updates. Compare to `agent_status.py` which has an optional status API key.