---
{
  "title": "WebSocket Connection Manager for Real-Time Chat",
  "summary": "Manages WebSocket connection lifecycle, user-to-connection mapping (supporting multi-tab/device), message routing to group members, typing indicators with auto-expiry, and presence tracking with grace periods. Exposed as a module-level singleton.",
  "concepts": [
    "ConnectionManager",
    "WebSocket",
    "connection lifecycle",
    "multi-tab support",
    "typing indicators",
    "auto-expiry",
    "presence tracking",
    "grace period",
    "message routing",
    "broadcast",
    "singleton"
  ],
  "categories": [
    "chat",
    "WebSocket",
    "real-time",
    "infrastructure"
  ],
  "source_docs": [
    "d49efa279d8cf7a5"
  ],
  "backlinks": null,
  "word_count": 565,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# WebSocket Connection Manager for Real-Time Chat

`cloud/chat/ws.py`

## Purpose

The `ConnectionManager` is the central hub for all real-time WebSocket communication. It tracks which users are online, routes messages to the right connections, and manages ephemeral state like typing indicators. It runs in-process (no external pub/sub) as a singleton.

## Core Data Structures

- `active_connections: dict[str, set[WebSocket]]` — Maps user IDs to their active WebSocket connections. A user can have multiple connections (multiple browser tabs, mobile + desktop).
- `_ws_to_user: dict[WebSocket, str]` — Reverse lookup from WebSocket to user ID. Needed during disconnect when the WebSocket object is all we have.
- `_offline_tasks: dict[str, asyncio.Task]` — Grace period timers before marking a user offline. If a user disconnects and reconnects within the grace period, they are never marked offline.
- `_typing_timers: dict[tuple[str, str], asyncio.Task]` — Auto-expiry timers for typing indicators, keyed by `(group_id, user_id)`.

## Connection Lifecycle

### connect(websocket, user_id)

Registers a new connection. If the user already has connections, the new one is added to the set. Cancels any pending offline timer — this handles the case where a user closes one tab and opens another quickly.

### disconnect(websocket) -> str | None

Removes a specific WebSocket connection. Returns the `user_id` only if this was their **last** connection — the caller (router) uses this to start a grace period before broadcasting offline status. If the user still has other connections, returns `None`.

This design prevents false offline notifications when a user just closed one of several tabs.

## Message Routing

### send_to_user(user_id, message)

Sends a message to ALL of a user's connections. Includes dead connection cleanup — if `send_json` raises an exception (broken pipe, closed socket), the connection is removed via `disconnect()`. This prevents stale connections from accumulating.

### broadcast_to_group(group_id, member_ids, message, exclude_user)

Iterates through group member IDs and sends to each online user. The `exclude_user` parameter prevents the sender from receiving their own message (they get a separate confirmation event).

## Typing Indicators

Typing state uses auto-expiry to handle the case where a user starts typing but never sends a message or explicitly stops. Without auto-expiry, a crashed client would leave a permanent "typing..." indicator.

- `start_typing(group_id, user_id)` — Cancels any existing timer and starts a new 5-second countdown.
- `stop_typing(group_id, user_id)` — Explicit stop, cancels the timer.
- `_typing_timeout(key)` — Async task that sleeps for `TYPING_TIMEOUT_SECONDS` then removes the entry.
- `is_typing(group_id, user_id)` — Check if a timer exists.

## Constants

- `TYPING_TIMEOUT_SECONDS = 5` — Typing indicator auto-expiry.
- `PRESENCE_GRACE_SECONDS = 30` — Grace period before marking offline (defined but not yet used in the manager).

## Singleton Pattern

`manager = ConnectionManager()` at module level creates a single instance shared across all request handlers. This works because FastAPI runs in a single process with async concurrency — all WebSocket handlers share the same event loop and can access the same manager.

## Known Gaps

- **Single-process only**: The in-memory connection tracking does not work across multiple server instances. A production deployment with multiple workers would need Redis pub/sub or similar for cross-process message routing.
- **Grace period not implemented**: `PRESENCE_GRACE_SECONDS` is defined but the actual offline grace period logic is not wired up — noted as "Task 19" in the router.
- **No connection limits**: No cap on connections per user — a runaway client could open unlimited WebSocket connections.
- **No heartbeat/ping**: No periodic ping to detect silently dead connections.
