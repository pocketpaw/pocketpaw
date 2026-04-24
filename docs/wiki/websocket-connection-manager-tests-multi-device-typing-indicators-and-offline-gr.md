---
{
  "title": "WebSocket Connection Manager Tests: Multi-Device, Typing Indicators, and Offline Grace Periods",
  "summary": "Tests for `ee.cloud.chat.ws.ConnectionManager`, the in-process registry that tracks active WebSocket connections per user, routes outbound messages, and manages typing indicators with auto-expiry timers. Key behavioral contracts include dead-connection cleanup, multi-device fan-out, idempotent stop-typing, and cancellation of offline grace period tasks on reconnect.",
  "concepts": [
    "ConnectionManager",
    "WebSocket",
    "multi-device",
    "typing indicator",
    "offline grace period",
    "dead connection cleanup",
    "asyncio task",
    "fan-out",
    "presence",
    "chat real-time"
  ],
  "categories": [
    "testing",
    "WebSocket",
    "real-time chat",
    "cloud API",
    "test"
  ],
  "source_docs": [
    "9f533de3e259f2ef"
  ],
  "backlinks": null,
  "word_count": 499,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ConnectionManager` is the stateful hub for PocketPaw's real-time chat layer. It maps user IDs to sets of WebSocket connections (supporting multiple simultaneous devices), tracks typing state per group/user, and manages asyncio tasks for offline grace periods. All tests use `AsyncMock` to simulate WebSocket objects without a real network.

## Multi-Device Connection Tracking

`test_multi_device` connects two WebSocket objects under the same `user_id` and asserts both appear in `get_user_connections`. This reflects PocketPaw's design requirement that a user can be active on a phone and a desktop simultaneously. `send_to_user_multi_device` then confirms that a single outbound message is fanned out to all connections, so neither device misses an event.

## Disconnect Semantics

`disconnect` returns the `user_id` only when the last connection for that user is removed (i.e., the user is now offline), or `None` if more connections remain. This return value is used by the calling code to trigger the offline grace period task — you only want to start the "user went offline" workflow when the *last* device disconnects, not when a user closes one of their tabs.

## Dead Connection Cleanup

`test_send_to_user_dead_connection_cleaned` sets `ws_dead.send_json.side_effect = RuntimeError` and calls `send_to_user`. The test asserts that the dead connection is removed from the active set while the healthy connection is kept. Without this cleanup, a single crashed WebSocket would accumulate indefinitely and block future sends to that user if the set is checked elsewhere.

## Typing Indicator State Machine

The typing system is a per-`(group_id, user_id)` state machine:

- `start_typing` sets the user as typing and starts (or restarts) an auto-expiry timer
- `stop_typing` clears the state and cancels the timer
- After the timeout (tested as 5 seconds in `test_typing_auto_expires`), the indicator automatically clears

`test_typing_restart_resets_timer` verifies that calling `start_typing` twice does not stack two timers. Without this guard, a user who types continuously would accumulate O(messages) pending asyncio tasks, eventually exhausting resources and firing multiple "stopped typing" events.

`test_typing_stop_idempotent` calls `stop_typing` when the user is not typing and confirms no exception is raised. This prevents the caller from needing to track whether it previously started a typing indicator.

## Offline Grace Period Cancellation

`test_connect_cancels_pending_offline_task` simulates a reconnect during a grace period: after disconnect, a fake 30-second sleep task is registered in `_offline_tasks["u1"]`. When `connect` is called again for the same user, the test asserts the task is cancelled and removed. Without this, a reconnecting user would still be marked offline by the grace period task, causing spurious "user went offline" events and incorrect presence indicators.

## No-Op Safety

`test_send_to_user_no_connections` calls `send_to_user` for a user with zero connections and verifies no exception is raised. `test_disconnect_unknown_ws` disconnects an unregistered WebSocket and expects `None` back. Both defend against race conditions where a connection disappears between the check and the operation.

## Known Gaps

The 5-second typing timeout is hardcoded in the test via `asyncio.sleep(6)`, making the test fragile if the timeout value is configurable. There are no tests for the broadcast behavior when a dead connection is encountered mid-broadcast (only `send_to_user` covers dead-connection cleanup).
