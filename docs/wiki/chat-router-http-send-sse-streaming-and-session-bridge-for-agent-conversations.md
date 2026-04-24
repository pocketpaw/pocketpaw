---
{
  "title": "Chat Router — HTTP Send, SSE Streaming, and Session Bridge for Agent Conversations",
  "summary": "The chat router enables external clients to drive PocketPaw agent sessions over plain HTTP, using Server-Sent Events for streaming responses. The `_APISessionBridge` class connects the internal message bus to an SSE stream, ensuring that chat sessions initiated via the HTTP API reuse the same AgentLoop pipeline as native channel adapters.",
  "concepts": [
    "chat API",
    "SSE streaming",
    "AgentLoop",
    "message bus",
    "OutboundMessage",
    "SystemEvent",
    "_APISessionBridge",
    "session bridge",
    "safe_key",
    "multi-tenant",
    "InboundMessage",
    "chat stop"
  ],
  "categories": [
    "API",
    "Chat",
    "Real-time"
  ],
  "source_docs": [
    "f130b3eea15631e4"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`chat.py` is the HTTP entry point for conversational interaction with a PocketPaw agent. It supports three operations: sending a message (POST), streaming the response (SSE GET), and cancelling an in-flight response (DELETE/POST stop). The key design goal is to reuse the existing `AgentLoop` pipeline rather than implementing a parallel execution path for HTTP clients.

## `_APISessionBridge`: Connecting Bus to SSE

The internal agent runtime communicates through a message bus (`OutboundMessage`, `SystemEvent`). SSE clients need a linear stream of `text/event-stream` events. `_APISessionBridge` translates between these two worlds:

```python
class _APISessionBridge:
    """Bridges the message bus to an asyncio.Queue for SSE streaming."""
```

On `start()`, it subscribes to both `OutboundMessage` (agent responses) and `SystemEvent` (tool events, errors) on the bus. Each message is pushed into an `asyncio.Queue`. The `_event_generator()` async generator drains the queue and yields SSE-formatted strings. On `stop()`, the subscriptions are removed and the queue is drained.

This architecture means the HTTP chat endpoint benefits automatically from every improvement made to the AgentLoop — streaming thinking tokens, tool-call events, error events — without any chat-specific logic.

## Session ID Mapping

PocketPaw identifies chat sessions internally by a `chat_id`. Clients are given a `safe_key` — a transformation of the raw chat ID — as their session identifier. The two helper functions `_extract_chat_id` and `_to_safe_key` maintain this bidirectional mapping. The indirection exists so that the internal `chat_id` format can change without breaking client-side session tokens.

## SSE Session Filter Fix (2026-02-25)

> Tighten SSE session filter: block events without session_key instead of silently passing them through to all clients.

Without this fix, a system event that lacked a `session_key` (e.g., a background health event) would be broadcast to every open SSE connection. This could cause clients to receive spurious events mid-conversation and misinterpret them as chat messages.

## Multi-Tenant Context Threading (2026-04-22)

> Thread cloud user + active_workspace from the authenticated request into `InboundMessage.metadata` so agent-created pockets land under the caller, not the first user in the DB.

In multi-tenant (cloud) deployments, the agent can create new pockets during a conversation. Without explicit context threading, the pocket would be attributed to the first user in the database — a tenant isolation bug. The fix reads the caller's identity from the JWT (via `_noop_user_dep` when `ee.cloud` is not mounted) and injects it into the message metadata.

## Chat Stop

`chat_stop` cancels an in-flight response by looking up the active session and signalling the AgentLoop to stop. The 2026-03-09 update reduced the blocking timeout from 3600s to 300s — a 3600-second stall is effectively a server hang, while 300s gives enough headroom for long-running tool chains.

## Known Gaps

The `_noop_user_dep` placeholder comment notes that this is a zero-dependency stub used when `ee.cloud` is not mounted. This means the cloud context injection silently no-ops in self-hosted deployments — acceptable for single-tenant use but worth monitoring if community editions gain multi-user support.