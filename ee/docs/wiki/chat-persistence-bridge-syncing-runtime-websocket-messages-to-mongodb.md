---
{
  "title": "Chat Persistence Bridge — Syncing Runtime WebSocket Messages to MongoDB",
  "summary": "Subscribes to PocketPaw's message bus outbound channel to capture agent streaming responses and persist them to MongoDB. Also provides save_user_message() for the WebSocket adapter to persist user messages. Ensures all chat history is durable regardless of which chat system originated the message.",
  "concepts": [
    "chat persistence",
    "message bus",
    "stream accumulation",
    "session auto-creation",
    "WebSocket bridge",
    "runtime to cloud sync",
    "in-memory cache"
  ],
  "categories": [
    "cloud",
    "shared",
    "chat",
    "persistence",
    "WebSocket"
  ],
  "source_docs": [
    "eab3f5ffe76abdfa"
  ],
  "backlinks": null,
  "word_count": 374,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Chat Persistence Bridge — Syncing Runtime WebSocket Messages to MongoDB

## Purpose

PocketPaw has two chat systems: the runtime WebSocket layer (file-based, local) and the cloud MongoDB layer. This bridge ensures messages flowing through the runtime system also get persisted to MongoDB, so the cloud dashboard can display complete chat history even for sessions that started as local WebSocket chats.

## Architecture

### Two Persistence Paths

1. **User messages** — `save_user_message()` is called directly by the WebSocket adapter when a user sends a message
2. **Agent messages** — `_on_outbound_message()` subscribes to the message bus outbound channel and accumulates streaming chunks

### Stream Accumulation

Agent responses arrive as a sequence of events:
- Multiple `is_stream_chunk` events — content fragments accumulated in `_stream_buffers[chat_id]`
- One `is_stream_end` event — triggers persistence of the accumulated buffer

Non-streaming messages (neither chunk nor end) are also accumulated into the buffer, handling the case where an agent backend sends complete messages without streaming.

### Session Auto-Creation

`_ensure_cloud_session()` lazily creates MongoDB Session and Group documents when a runtime WebSocket chat first needs persistence. The flow:

1. Check `_active_sessions` in-memory cache
2. Look up existing Session by `sessionId = "websocket_{chat_id}"`
3. If no session exists, find the first available user/workspace and create both a Group and Session

The in-memory cache (`_active_sessions`) avoids repeated database lookups for the same chat. This is a process-local cache — it resets on restart, but the database lookup handles that case.

### Workspace Discovery

When creating a new cloud session for a runtime chat, the bridge queries for the first user with a non-empty workspaces list. This is a pragmatic shortcut for single-user or dev deployments but could assign the wrong workspace in multi-tenant scenarios.

## Known Gaps

- `_active_sessions` and `_stream_buffers` are module-level dicts — no cleanup mechanism for finished sessions, potential memory leak in long-running processes
- Workspace discovery uses `User.find({"workspaces": {"$ne": []}}).limit(1)` — picks an arbitrary user in multi-user setups
- No deduplication guard — if the message bus delivers a chunk twice, the buffer accumulates it twice
- Stream end handler updates `messageCount` and `lastActivity` directly on the Session document — duplicates the logic in `SessionService.touch()`
- All errors are caught and logged at `debug` level — persistence failures are silent in production
