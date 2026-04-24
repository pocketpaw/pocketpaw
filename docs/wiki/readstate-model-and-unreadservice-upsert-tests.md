---
{
  "title": "ReadState Model and UnreadService Upsert Tests",
  "summary": "This module tests the ReadState model's default values and the atomic upsert behavior of UnreadService.mark_read and bump_mention against a real in-memory MongoDB. The tests enforce the unique (user, group) index constraint and verify that concurrent operations remain idempotent.",
  "concepts": [
    "ReadState",
    "UnreadService",
    "mark_read",
    "bump_mention",
    "upsert",
    "unique index",
    "mention_unread",
    "idempotency",
    "beanie_memory_db",
    "in-memory MongoDB",
    "field isolation"
  ],
  "categories": [
    "testing",
    "data model",
    "chat",
    "unread tracking",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_read_state.py"
  ],
  "backlinks": null,
  "word_count": 494,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_read_state.py` covers the data layer for tracking which messages a user has read and how many unread mentions they have. These tests run against the `beanie_memory_db` fixture (in-memory Mongo) so the actual upsert queries are exercised rather than mocked. The critical properties under test are atomicity, idempotency, and field isolation.

## ReadState Model Defaults

`test_read_state_defaults` verifies the model's initial state:
- `mention_unread` starts at 0 — a freshly created ReadState has no pending mentions
- `last_read_at` is a `datetime` instance, defaulting to the current time

These defaults matter because the unread calculation logic branches on whether a ReadState row exists and what its `last_read_at` is.

## mark_read: Upsert Semantics

### Creates a Row When Missing
`test_mark_read_creates_row_when_missing` calls `UnreadService.mark_read("u1", "g1", "m5")` on an empty DB and then asserts that exactly one `ReadState` document exists with `last_read_message_id = "m5"` and `mention_unread = 0`. This tests the insert-on-miss path of the upsert.

### Updates and Zeros Mentions
`test_mark_read_updates_existing_row_and_zeros_mention_unread` seeds a row with pending mentions, then calls `mark_read`. The assertions check:
1. The `last_read_message_id` is updated to the new value
2. `mention_unread` is reset to 0

Resetting mentions on mark_read is correct because the user has now read up to that point, implicitly acknowledging the mentions. Without this zero-out, badge counts would remain stale.

### Idempotency Under Repeat Calls
```python
async def test_mark_read_is_idempotent_under_repeat():
    # Calling mark_read twice in a row must not produce duplicate rows.
```

This test calls `mark_read` twice with the same parameters and then counts the resulting documents in the collection. The expected count is exactly 1. Without the unique `(user, group)` index and upsert semantics, a naive implementation would insert two rows, corrupting the unread state.

## bump_mention: Increment-Only Semantics

### Creates Row with Empty last_read
`test_bump_mention_creates_row_with_empty_last_read` verifies the insert-on-miss path for `bump_mention`. The new row must have `mention_unread = 1` and an empty `last_read_message_id`. A user who has never read a group but receives a mention needs a row seeded with no read position.

### Increments Existing Counter
`test_bump_mention_increments_existing_counter` calls `bump_mention` twice and asserts `mention_unread == 2`. This tests that the operation is additive, not a set-to-1 operation.

### Does Not Overwrite last_read
```python
async def test_bump_mention_does_not_overwrite_last_read():
    # If a user already acked a message, bumping their mention counter
    # must not reset last_read_message_id.
```

This is the field-isolation test. `bump_mention` must use a targeted `$inc` update that touches only `mention_unread`, leaving `last_read_message_id` and `last_read_at` untouched. A naive implementation that replaces the whole document would lose the user's read position, causing phantom unreads.

## Why In-Memory Mongo Instead of Mocks

The upsert, `$inc`, and unique-index behaviors are all MongoDB-specific. Unit tests with mocked collections cannot detect a regression in the query (e.g., switching from `upsert=True` to a plain `insert`). The `beanie_memory_db` fixture provides real query semantics at the cost of slightly slower startup.

## Known Gaps

No TODOs or FIXMEs are present. Tests do not cover concurrent upserts from multiple asyncio tasks, which could expose write-conflict behavior in the upsert implementation.
