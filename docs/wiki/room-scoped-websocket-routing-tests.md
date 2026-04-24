---
{
  "title": "Room-Scoped WebSocket Routing Tests",
  "summary": "This module tests the room-scoped routing layer in ConnectionManager, which limits typing indicators and read receipts to only the sockets currently joined to a specific group. It also verifies that non-members cannot inject typing or read-ack events into rooms they do not belong to.",
  "concepts": [
    "ConnectionManager",
    "room scoping",
    "WebSocket routing",
    "typing indicators",
    "read receipts",
    "membership enforcement",
    "send_to_room",
    "sender exclusion",
    "join_room",
    "leave_room",
    "non-member spoofing"
  ],
  "categories": [
    "testing",
    "WebSocket",
    "security",
    "chat",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_room_scoped.py"
  ],
  "backlinks": null,
  "word_count": 509,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_room_scoped.py` covers the room-join/leave/routing layer added to the `ConnectionManager`. Room scoping prevents chat events — specifically typing indicators and read receipts — from leaking to or being spoofed by users in the wrong group. Without room scoping, a typing indicator would either broadcast to every connected socket or require the client to filter events, both of which are correctness and privacy problems.

## ConnectionManager Room State

### Single Current Room per Socket
`test_join_room_tracks_single_current_room` establishes the invariant that each WebSocket connection can be in at most one room at a time:

```python
mgr.join_room(ws, "g1")
assert mgr.current_room(ws) == "g1"
mgr.join_room(ws, "g2")
assert mgr.current_room(ws) == "g2"  # replaces, not appends
```

This one-room-per-socket model is intentional: a user viewing a specific group channel should receive typing and read events only for that channel. If multiple rooms were tracked simultaneously, the client would receive typing events for groups not currently in view.

### leave_room Clears State
`test_leave_room_clears_current_room` verifies that `leave_room` sets the current room to `None`. This matters for the delivery filter: a socket with no current room receives no room-scoped messages.

### Disconnect Clears Room
`test_disconnect_clears_current_room` verifies that disconnecting a socket also clears its room assignment, preventing stale room mappings from accumulating in the manager's internal state.

## Delivery Filtering

### send_to_room Only Reaches Joined Sockets
`test_send_to_room_only_delivers_to_joined_sockets` creates two sockets, joins only one to `"g1"`, sends a message to `"g1"`, and asserts that only the joined socket received it. The un-joined socket's `send_json` is never called.

### Sender Exclusion
`test_send_to_room_excludes_user` verifies the self-exclusion path: when the sender is excluded from delivery (as is common for typing indicators — you don't need to see your own typing), the sender's socket does not receive the message even if they are in the room.

## Access Control: Non-Members Cannot Join or Emit

Three pairs of tests cover the membership enforcement layer:

### room.join
- `test_room_join_rejects_non_member` — a user not in the group cannot join the room WebSocket room
- `test_room_join_allows_member` — a group member can successfully join

### typing
- `test_typing_rejects_non_member` — a non-member cannot spoof a typing indicator into the group (which would show their name in other users' UIs)
- `test_typing_allows_member` — a real member's typing event is broadcast to the room

### read_ack
- `test_read_ack_rejects_non_member` — a non-member cannot send a fake read receipt (which could manipulate unread counts)
- `test_read_ack_allows_member` — a real member's read receipt is accepted and broadcast

The access control tests use `monkeypatch` to control the membership check, isolating the routing logic from the database layer.

## Why Room Scoping Exists at the WebSocket Layer

An alternative design would have clients self-filter events by group ID. This is fragile: a bug in client code could display events from the wrong room, and a malicious client could ignore the filter entirely. Enforcing room scoping server-side means the server never sends events to sockets that should not receive them.

## Known Gaps

No TODOs or FIXMEs are present. Tests do not cover the behavior when a group is archived mid-session while a socket is still joined to it.
