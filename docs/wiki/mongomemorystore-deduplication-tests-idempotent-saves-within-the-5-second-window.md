---
{
  "title": "MongoMemoryStore Deduplication Tests: Idempotent Saves Within the 5-Second Window",
  "summary": "This module tests the deduplication logic in `MongoMemoryStore`, which prevents identical back-to-back writes from creating duplicate memory entries when an agent loop retries the same turn. The dedup key is `(session_key, role, content)` with a 5-second window, and the tests verify that only different content, session, or role values produce distinct database rows.",
  "concepts": [
    "deduplication",
    "MongoMemoryStore",
    "idempotent writes",
    "session_key",
    "memory entry",
    "agent loop retries",
    "dedup window",
    "MemoryEntry",
    "MemoryType.SESSION",
    "write guard"
  ],
  "categories": [
    "Cloud Memory",
    "Testing",
    "Idempotency",
    "MongoDB",
    "test"
  ],
  "source_docs": [
    "3cd6d7754e604d33"
  ],
  "backlinks": null,
  "word_count": 575,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/memory/test_dedup.py` tests the write-idempotency guard built into `MongoMemoryStore`. The module docstring states the motivation directly:

> Guards against agent-loop retries of the same turn landing two rows -- the dedup window (5s) is short enough that legitimate back-to-back "ok" messages still persist separately, but long enough to absorb a synchronous in-request duplicate.

## The Problem Being Solved

PocketPaw's agent runtime processes conversation turns and persists each message to `MongoMemoryStore`. Under certain failure conditions -- network timeouts, request retries from the frontend, or agent-loop restarts -- the same message can be submitted twice in rapid succession. Without a dedup layer, both submissions create separate rows, and when the agent recalls its memory it sees the same message twice, distorting the conversation history.

A 5-second dedup window is the right granularity because:
- Humans rarely type two identical messages within 5 seconds of each other (the window is short enough to not suppress legitimate duplicates).
- Synchronous retries from the same request always complete within 5 seconds.
- The agent loop's retry backoff is longer than 5 seconds for genuine failures.

## The Dedup Key

The dedup check keys on `(session_key, role, content)`. All three dimensions must match to be considered a duplicate:

- **`session_key`**: scopes the dedup to a single conversation session. The same content in two different sessions is not a duplicate.
- **`role`**: `"user"` and `"assistant"` saying the same thing are distinct events.
- **`content`**: different content in the same session from the same role is never a duplicate.

## Test Breakdown

### `test_second_save_with_same_content_reuses_existing_id`

```python
first_id = await store.save(_entry(key, "user", "hello there"))
second_id = await store.save(_entry(key, "user", "hello there"))
assert first_id == second_id
```

The second save must return the same ID as the first, confirming that no second row was inserted. The `store` fixture provides a fresh `MongoMemoryStore` per test, so the only match can come from the first save within this test.

### `test_different_content_is_not_deduped`

Two saves with the same session and role but different content (`"first"` vs `"second"`) must produce different IDs -- each is a distinct memory entry.

### `test_different_session_is_not_deduped`

Same content and role, but in two different sessions (`key_a` vs `key_b`). Must produce different IDs. Without this, a common phrase like `"ok"` from one user's session would be deduplicated against the same phrase from another user's session.

### `test_different_role_is_not_deduped`

Same session and content, but different roles (`"user"` vs `"assistant"`). Must produce different IDs. A user saying `"ping"` and the assistant responding `"ping"` are two distinct turns that must both be recorded.

## Implementation Inference

Although the implementation is not shown, the tests imply:

1. `store.save` returns a string ID.
2. Before inserting, the store queries for an existing document matching `(session_key, role, content)` created within the last 5 seconds.
3. If found, the existing document's ID is returned without inserting.
4. If not found, a new document is inserted and its ID is returned.

The use of `pytest.mark.asyncio` is implicit -- the test class methods are `async def` without the decorator, which means the test suite is configured with `asyncio_mode = "auto"` in `pytest.ini` or `pyproject.toml`.

## Known Gaps

There are no tests for the time boundary of the 5-second window -- specifically, what happens when the first save is just over 5 seconds old. Testing this would require either a controllable clock (similar to the tree cache tests) or a `time.sleep` call, neither of which appears here. The window boundary behavior is implicitly trusted from the implementation.
