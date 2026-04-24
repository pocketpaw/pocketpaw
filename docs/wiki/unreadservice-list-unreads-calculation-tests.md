---
{
  "title": "UnreadService List Unreads Calculation Tests",
  "summary": "This module tests UnreadService.list_unreads, which computes per-group unread message and mention counts for a user. Tests cover the caught-up state, partial reads, the fresh-user fallback, and the edge case where a ReadState row exists but has no recorded read position.",
  "concepts": [
    "UnreadService",
    "list_unreads",
    "ReadState",
    "message_count",
    "mention_unread",
    "fresh user fallback",
    "empty last_read_message_id",
    "bump_mention edge case",
    "patch mocking",
    "unread calculation"
  ],
  "categories": [
    "testing",
    "unread tracking",
    "chat",
    "data model",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_unread_service.py"
  ],
  "backlinks": null,
  "word_count": 440,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_unread_service.py` covers `UnreadService.list_unreads`, the function that answers: "how many unread messages does this user have in each of their groups?" The tests use patch-based mocks for the three internal helpers (`_list_member_groups`, `_get_read_state`, `_count_messages_after`) so each test can precisely control the DB state without needing a real database.

## Calculation Logic Under Test

`list_unreads` combines three data sources:
1. The groups the user belongs to (from `_list_member_groups`)
2. The user's read state per group (from `_get_read_state`)
3. The count of messages posted after the user's last read (from `_count_messages_after`)

The tests exercise the branches in this calculation.

## Test Cases

### Caught-Up State (Zero Unread)
`test_list_unreads_zero_when_caught_up` sets `last_read_message_id = "m5"` and `_count_messages_after` returning 0. The expected result is `{"unread": 0, "mention_unread": 0}`. This is the happy path — a user who has read everything.

### Partial Read (Counted Unreads)
`test_list_unreads_counts_messages_after_last_read` sets `message_count = 10`, `last_read_message_id = "m5"`, `mention_unread = 2`, and `_count_messages_after` returning 5. The expected result is `{"unread": 5, "mention_unread": 2}`. This confirms that the service uses the live count rather than the stale `message_count` field on the group document.

### Fresh User (No ReadState Row)
`test_list_unreads_fresh_user_has_full_count` mocks `_get_read_state` returning `None` (no row exists). The expected result is `{"unread": group.message_count}`. When a user has never read a group, the entire message history is unread. Falling back to `message_count` avoids an expensive full-collection count for this common case.

### Empty last_read_message_id (Bump-Only State)
```python
async def test_list_unreads_empty_last_read_falls_through_to_message_count():
    # If a ReadState row exists but last_read_message_id is '' (created by
    # bump_mention before the user has ever read), fall through to message_count.
```

This edge case arises because `bump_mention` creates a ReadState row with an empty `last_read_message_id` before the user has ever opened the group. The service must handle this gracefully — it cannot call `_count_messages_after("")` (the empty string is not a valid message ID) and must instead fall back to `group.message_count`. Without this guard, the unread count calculation would either error or return a nonsensical value for users who received a mention in a group they have never visited.

## Why These Are Unit Tests (Not Integration Tests)

The three internal helpers each involve a separate DB query. By mocking them, the tests pin the calculation logic independently of query correctness. Integration tests for the DB layer (covering `_count_messages_after` with real Mongo) live elsewhere. This separation keeps failure diagnosis fast: a broken test here points to the calculation logic, not the query.

## Known Gaps

No TODOs or FIXMEs are present. The test suite does not cover multi-group aggregation (user in 5 groups, mixed unread states), which would be a useful regression test for the loop logic in `list_unreads`.
