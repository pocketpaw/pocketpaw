---
{
  "title": "Notification Service: CRUD and Realtime Fan-Out",
  "summary": "The `NotificationService` is a stateless class providing four async operations — create, list, mark-read, and clear-all — each of which writes to MongoDB via Beanie and immediately emits a corresponding realtime event for connected WebSocket clients. The service is the single point of truth for notification lifecycle, keeping routers and other domain services free of MongoDB and emit logic.",
  "concepts": [
    "NotificationService",
    "stateless service",
    "CRUD",
    "realtime fan-out",
    "emit",
    "idempotency",
    "ownership check",
    "bulk update",
    "Beanie",
    "WebSocket events"
  ],
  "categories": [
    "notifications",
    "services",
    "realtime",
    "enterprise-cloud"
  ],
  "source_docs": [
    "f4fa8f44d5202ef9"
  ],
  "backlinks": null,
  "word_count": 546,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`NotificationService` orchestrates the full lifecycle of in-app notifications: persistence, querying, state mutation, and realtime push. By wrapping all four operations in one class, every notification-producing part of the system (mention handling, agent completion, invite flow) has a single, consistent interface.

## The Stateless Class Pattern

All methods are `@staticmethod` — `NotificationService` has no instance state and is never instantiated. This is a deliberate choice for a service that wraps database operations: it avoids dependency injection complexity and makes the service trivially importable and callable from anywhere without a factory or context manager. The tradeoff is that the service cannot be mocked at the instance level; tests must patch at the method or database level.

## `create`: Insert and Emit

```python
notif = Notification(...)
await notif.insert()
await emit(NotificationNew(data=_to_wire(notif)))
return notif
```

The emit follows the insert atomically (within the same async task, no transaction). If the emit fails, the notification is already persisted — the client will see it on the next poll. If the insert fails, the emit never fires, which is correct. This "emit after persist" ordering ensures clients never receive a push for a notification they cannot later fetch.

## `list_for_user`: Filtered and Sorted

The query chains `.sort(-Notification.createdAt).limit(limit)`. Sorting descending by `createdAt` returns the most recent notifications first, which is the expected order for a notification panel. The optional `unread=True` filter adds `"read": False` to the Beanie query dict, exploiting the `(recipient, read, created_at DESC)` compound index for covered queries.

## `mark_read`: Ownership Check and Idempotency

```python
if not notif or notif.recipient != user_id:
    return
if notif.read:
    return
```

Two guards before any mutation:

1. **Ownership**: a notification that does not exist or belongs to another user is silently ignored. Returning `None` (not raising `403`) prevents attackers from using the error response as an oracle to discover valid notification IDs.
2. **Idempotency**: if already read, the method exits without touching the database or emitting an event. This makes the endpoint safe to retry without generating duplicate `NotificationRead` events on the realtime bus.

## `clear_all`: Bulk Update with Count

```python
result = await Notification.find({"recipient": user_id, "read": False}).update_many(
    {"$set": {"read": True}}
)
await emit(NotificationCleared(data={"user_id": user_id}))
return getattr(result, "modified_count", 0)
```

Using `update_many` rather than iterating and saving individually is critical for performance — a user with 500 unread notifications would otherwise require 500 round-trips. The `getattr(result, "modified_count", 0)` defensive access handles the case where Beanie's `update_many` returns an object without `modified_count` on certain driver versions.

## `_to_wire` Serializer

The module-level `_to_wire` function converts a `Notification` document to the dict shape the router returns. It uses `iso_utc()` from `ee.cloud.shared.time` for consistent UTC ISO 8601 timestamp formatting, and falls back gracefully on missing `source` (`n.source.id if n.source else None`).

## Known Gaps

- No database transaction wrapping `insert` + `emit`: if the process crashes between the two, the notification is persisted but no realtime push fires. Clients that reconnect will see it on the next poll, so this is a latency gap rather than a data-loss gap.
- `clear_all` emits a single `NotificationCleared` event regardless of how many notifications were cleared, so clients cannot selectively update individual notification states from the event alone — they must re-fetch the list.
- No retry logic on the `emit` calls; a transient realtime bus failure silently drops the push.