---
{
  "title": "Broadcast Reminder Persistence Tests: Session History Durability Across WebSocket Connections",
  "summary": "This test suite verifies the fix for GitHub issue #364, where reminder messages fired by the scheduler were broadcast over WebSocket but never saved to session history, causing them to disappear after a tab switch or page reload. Tests confirm that `broadcast_reminder()` persists each reminder to every active WebSocket session's history and that a persistence failure does not block the broadcast.",
  "concepts": [
    "broadcast_reminder",
    "WebSocket",
    "session history",
    "reminder persistence",
    "dashboard_lifecycle",
    "memory manager",
    "fault isolation",
    "issue #364",
    "scheduler",
    "add_to_session"
  ],
  "categories": [
    "reminders",
    "testing",
    "WebSocket",
    "memory",
    "bug fixes",
    "test"
  ],
  "source_docs": [
    "b597da086e78c95e"
  ],
  "backlinks": null,
  "word_count": 401,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's reminder system allows users to schedule future notifications. These reminders are fired by a background scheduler and delivered via WebSocket to the dashboard. Before the fix tracked by issue #364, the reminder message was broadcast live but not written to the session's message history, so it was visible only if the user happened to be on that browser tab at the exact moment of firing. A tab switch or reload would make the reminder disappear.

The fix writes the reminder to `memory_manager.add_to_session()` for every active WebSocket connection whenever `broadcast_reminder()` fires. This test file validates that fix exhaustively.

## Core Persistence Contract

```python
async def test_persists_to_single_active_session(self):
    reminder = {"id": "r1", "text": "call mom"}
    await broadcast_reminder(reminder)
    mock_manager.add_to_session.assert_awaited_once_with(
        session_key="websocket:abc123",
        role="assistant",
        content="Reminder: call mom",
        metadata={"reminder_id": "r1", "type": "reminder"},
    )
```

The session key format `websocket:<connection_id>` namespaces reminder history under the WebSocket session. The `role="assistant"` assignment makes the reminder appear as if PocketPaw itself delivered it, which is correct since reminders are agent-initiated messages.

## Multi-Session Fan-out

```python
async def test_persists_to_multiple_active_sessions(self):
    mock_ws_adapter._connections = {"chat1": ..., "chat2": ...}
    await broadcast_reminder(reminder)
    assert mock_manager.add_to_session.await_count == 2
    called_keys = {call.kwargs["session_key"] for call in ...}
    assert called_keys == {"websocket:chat1", "websocket:chat2"}
```

If multiple browser tabs are open, each gets its own history entry. Without this, a user with two open tabs would only see the reminder in one of them after a reload.

## Fault Isolation: Persistence Failure Must Not Block Broadcast

```python
async def test_persist_failure_does_not_prevent_broadcast(self):
    mock_manager.add_to_session = AsyncMock(side_effect=RuntimeError("disk full"))
    await broadcast_reminder(reminder)  # Must not raise
    mock_ws_adapter.broadcast.assert_awaited_once()
```

This is the most important defensive test. The persistence call is best-effort — if the database or memory store is temporarily unavailable, the live WebSocket broadcast must still fire. A transient disk error should not cause the scheduler to silently drop the user's reminder.

## Content Format Consistency

```python
async def test_reminder_content_format(self):
    assert call_kwargs["content"] == "Reminder: drink water"
    assert call_kwargs["metadata"]["type"] == "reminder"
    assert call_kwargs["metadata"]["reminder_id"] == "r5"
```

The content format `"Reminder: <text>"` matches what the frontend displays, ensuring the history replay looks identical to the live delivery. The `reminder_id` in metadata enables future features like dismissal that need to reference the original reminder record.

## Known Gaps

All tests use `from __future__ import annotations` but async methods lack `@pytest.mark.asyncio` decorators — this is only valid if the test runner is configured with `asyncio_mode = "auto"`. No explicit test covers what happens if `get_memory_manager()` itself raises (as opposed to `add_to_session()` raising).