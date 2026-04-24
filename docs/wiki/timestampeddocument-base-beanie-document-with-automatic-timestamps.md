---
{
  "title": "TimestampedDocument — Base Beanie Document with Automatic Timestamps",
  "summary": "A Beanie `Document` base class that automatically manages `createdAt` and `updatedAt` timestamps using Beanie's event hook system. All cloud ODM documents inherit from this class to get consistent, server-side timestamp management.",
  "concepts": [
    "TimestampedDocument",
    "Beanie ODM",
    "createdAt",
    "updatedAt",
    "before_event",
    "Insert hook",
    "Replace hook",
    "Update hook",
    "use_state_management",
    "UTC timestamps",
    "base document"
  ],
  "categories": [
    "data modeling",
    "MongoDB",
    "Beanie",
    "architecture"
  ],
  "source_docs": [
    "cfa3cc57b97f98e2"
  ],
  "backlinks": null,
  "word_count": 403,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`base.py` provides `TimestampedDocument`, the base class for every Beanie document in the enterprise cloud layer. Its only responsibility is timestamp management — ensuring that `createdAt` is set once on insert and `updatedAt` is refreshed on every subsequent save.

## Why Server-Side Timestamps

Timestamps set on the client (browser or API caller) are susceptible to clock skew, timezone errors, and deliberate manipulation. Server-side timestamps set in the Beanie event hooks are authoritative — they reflect the actual time the database write occurred, not when the client submitted the request. This matters for audit trails, feed ordering, and time-based queries.

## Implementation

```python
class TimestampedDocument(Document):
    createdAt: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updatedAt: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @before_event(Insert)
    def _set_created(self):
        now = datetime.now(UTC)
        self.createdAt = now
        self.updatedAt = now

    @before_event(Replace, Save, Update)
    def _set_updated(self):
        self.updatedAt = datetime.now(UTC)

    class Settings:
        use_state_management = True
```

## Event Hook Design

The `@before_event(Insert)` hook fires before any insert and sets both timestamps to the same value. This ensures that a document created and immediately queried will have matching `createdAt` and `updatedAt`, which is the expected invariant.

The `@before_event(Replace, Save, Update)` hook covers all mutation events. Using multiple event types in a single decorator means the hook fires regardless of which Beanie save pattern is used — `await doc.save()`, `await doc.replace()`, or `await Model.update()` calls all trigger `_set_updated`.

The `default_factory` on both fields provides a sensible fallback in case a document is constructed and saved without going through the Beanie insert path (e.g., in unit tests that bypass the ODM). The UTC-aware `datetime.now(UTC)` uses the post-Python-3.11 timezone-aware form.

## use_state_management

The `Settings.use_state_management = True` flag enables Beanie's change-tracking feature, which detects field-level changes before issuing partial updates. This is required for `@before_event(Update)` hooks to fire correctly — without it, update operations bypass the event system.

## Naming Convention

The fields are named `createdAt` / `updatedAt` (camelCase) rather than the Python-idiomatic `created_at` / `updated_at`. This matches the JavaScript/MongoDB convention used by the frontend and by the existing documents already in the database at the time this base class was introduced. Changing to snake_case would require a data migration.

## Known Gaps

- `_set_updated` does not check whether any fields actually changed. A no-op `await doc.save()` will still bump `updatedAt`. This can cause false positives in feed-based UIs that sort by `updatedAt`.
- There is no `deleted_at` field for soft deletes — documents that need audit-trail deletion must implement this field themselves.