---
{
  "title": "NotificationService Unit Tests: CRUD, Fan-Out, and Idempotency Contract",
  "summary": "These unit tests exercise NotificationService in isolation by patching the Notification Beanie document and the emit function, verifying that create persists then emits, list_for_user filters at the database layer, mark_read is idempotent for already-read notifications, and clear_all uses a bulk update and emits a single event.",
  "concepts": [
    "NotificationService",
    "CRUD",
    "real-time fan-out",
    "emit",
    "NotificationNew",
    "NotificationRead",
    "NotificationCleared",
    "mark_read idempotency",
    "clear_all",
    "AsyncMock",
    "bulk update"
  ],
  "categories": [
    "testing",
    "notifications",
    "real-time",
    "idempotency",
    "test"
  ],
  "source_docs": [
    "6ffdb699a1c93d06"
  ],
  "backlinks": null,
  "word_count": 312,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`NotificationService` persists in-app notifications to MongoDB via Beanie and fans them out to connected clients via the real-time event bus. These tests patch both layers so they run without a database or live WebSocket, making them fast and deterministic.

## Create: Persist Then Emit

```python
async def test_create_persists_and_emits_notification_new():
    result = await NotificationService.create(
        workspace_id="w1", recipient="u2", kind="mention",
        title="You were mentioned", body="hello")
    fake.insert.assert_awaited_once()
    assert isinstance(recorded[0], NotificationNew)
    assert recorded[0].data["read"] is False
```

The emit fires **after** the insert. If emit failed before persist, the client would see a notification that doesn't exist in the database. With this ordering, a failed emit results in a stored but undelivered notification — the client can poll or reconnect.

## List: Database-Layer Filter Verification

```python
async def test_list_for_user_filters_unread():
    results = await NotificationService.list_for_user("u1", unread=True, limit=25)
    assert seen_query == {"recipient": "u1", "read": False}
```

The test captures the raw MongoDB query dict passed to `Notification.find`. This is more reliable than checking results alone — it verifies the filter is applied at the database layer, not in Python after fetching everything.

## Mark Read: Idempotency Guard

```python
async def test_mark_read_noop_for_already_read():
    notif = _fake_notification(notif_id="n1", recipient="u1", read=True)
    await NotificationService.mark_read("n1", "u1")
    notif.save.assert_not_awaited()
    assert recorded == []
```

If a notification is already read, the service must not call `save()` and must not emit `NotificationRead`. Without this guard, a race condition (two clients marking read simultaneously) would cause redundant database writes and spurious real-time events.

## Clear All: Bulk Operation

```python
async def test_clear_all_emits_notification_cleared():
    count = await NotificationService.clear_all("u1")
    assert count == 3
    assert isinstance(recorded[0], NotificationCleared)
    assert recorded[0].data == {"user_id": "u1"}
```

`clear_all` uses MongoDB's `update_many` for efficiency rather than per-notification updates. A single `NotificationCleared` event keeps WebSocket traffic proportional to users, not notification counts.

## Fake Notification Pattern

The `_fake_notification` helper creates a `SimpleNamespace` with `insert` and `save` as `AsyncMock` instances, avoiding Beanie initialization while still allowing method call assertions.

## Known Gaps

None identified.