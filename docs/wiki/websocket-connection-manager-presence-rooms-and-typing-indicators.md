---
{
  "title": "WebSocket Connection Manager - Presence, Rooms, and Typing Indicators",
  "summary": "The `ConnectionManager` class maintains the in-memory registry of active WebSocket connections, manages presence state with a 30-second grace window, routes messages to group rooms, and expires typing indicators automatically after 5 seconds. It is the real-time backbone for the chat domain.",
  "concepts": [
    "ConnectionManager",
    "WebSocket",
    "presence",
    "grace period",
    "typing indicators",
    "multi-tab",
    "room routing",
    "JWT authentication",
    "in-memory registry",
    "PresenceOnline",
    "PresenceOffline",
    "broadcast"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "WebSocket",
    "realtime"
  ],
  "source_docs": [
    "0d3fdf333d77f2ae"
  ],
  "backlinks": null,
  "word_count": 467,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ws.py` provides `ConnectionManager`, the single in-memory authority for all live WebSocket connections. Every capability that requires knowing which users are currently connected - presence detection, message routing to room members, typing indicators - flows through this class.

The endpoint it serves is `ws://host/ws/cloud?token=<JWT>`. JWT authentication via query parameter (rather than headers) is a browser constraint - the WebSocket API does not allow custom request headers.

## Connection Lifecycle

```
connect(websocket, user_id)
  -> add to active_connections[user_id]
  -> if first connection for user: fire PresenceOnline
disconnect(websocket, user_id)
  -> remove from active_connections[user_id]
  -> if last connection for user: schedule PresenceOffline after 30s
```

The first/last connection logic is essential for multi-tab support. A user with three browser tabs open has three WebSocket connections. Closing one tab must not trigger an offline event. Only when the last connection closes is the user considered offline - and even then, the 30-second grace window allows quick page reloads to be invisible to other users.

## Presence Grace Window

The 30-second grace period prevents presence flapping during reload cycles. Without it, a page refresh would produce a rapid offline-then-online transition visible to all workspace members. The delayed task is cancelled if the user reconnects within the window - implementing debounced presence without a distributed timer service.

## Room-Based Message Routing

Alongside the user-keyed connection map, `ConnectionManager` maintains a room map: `room_id -> set[WebSocket]`. When a client joins a group, `join_room` registers the socket in that room. `send_to_room` delivers to all sockets in the room, with an optional `exclude_user` parameter to prevent the sender from receiving their own message as a duplicate.

The room model is more efficient than the alternative of looking up group members from the database on every broadcast - membership is resolved once at join time and cached in memory.

## Typing Indicators

Typing indicators are ephemeral state that must self-expire. If a user starts typing and then closes the tab, there is no disconnect event path that clears the indicator - it would hang indefinitely. The `_typing_timeout` coroutine is scheduled when `start_typing` is called; if the client sends `stop_typing` first, the pending task is cancelled. After 5 seconds of inactivity the indicator clears automatically.

## Multi-Tab / Multi-Device

`active_connections` maps `user_id -> set[WebSocket]`. `send_to_user` iterates the set and delivers to every device. Stale connections (sockets that closed without a clean disconnect) are caught by `try/except` on send, and the dead socket is removed from the registry at that point.

## Known Gaps

- The connection registry is in-process memory. In a horizontally scaled deployment, a user's connections may be spread across processes. Cross-process presence and message routing would require a shared pub/sub layer such as Redis.
- JWT-in-query-param means tokens appear in access logs.
- The 30-second grace period and 5-second typing timeout are module-level constants, not configurable.