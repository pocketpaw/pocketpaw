---
{
  "title": "MessageService Realtime Emit Tests",
  "summary": "This test module enforces that every public mutation in MessageService fires the correct typed realtime event through emit(). It also includes regression guards ensuring the WebSocket router itself does not perform direct broadcasts that would bypass the event bus.",
  "concepts": [
    "MessageService",
    "realtime events",
    "emit",
    "MessageNew",
    "MessageSent",
    "mention fan-out",
    "@everyone deduplication",
    "unread updates",
    "inline replies",
    "thread_count",
    "regression guard",
    "pytest-asyncio"
  ],
  "categories": [
    "testing",
    "realtime",
    "chat",
    "messaging",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_message_emits.py"
  ],
  "backlinks": null,
  "word_count": 476,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_message_emits.py` is the emit contract test for `MessageService`. The philosophy mirrors the group emit tests: every mutation must produce the right event, and tests patch only the DB/permission seam so the emit wiring is exercised in full isolation.

## Core Mutation Coverage

### send_message
The `test_send_message_emits_new_and_sent` test asserts that sending a message fires **two** events:
- `MessageNew` — broadcast to the group, hydrating message lists for all connected clients
- `MessageSent` — scoped to the sender, used for delivery confirmation UI

Both events must fire on a single `send_message` call. Missing either would break either the sender's confirmation flow or the group's live feed.

### edit_message / delete_message / toggle_reaction
Each gets its own test asserting a single typed event (`MessageEdited`, `MessageDeleted`, `MessageReaction`). These are straightforward but must be tested explicitly — a developer refactoring the service could accidentally drop an emit call.

## Regression Guard: Router Must Not Broadcast

```python
def test_router_no_longer_broadcasts_message_events():
    # Regression guard: the four _ws_message_* handlers must not call
    # manager.broadcast/send.
```

This test exists because message events were historically broadcast directly from the WebSocket router. That design was replaced by the event bus so routing logic lives in one place. The test scans the four `_ws_message_*` handler functions for any call to `manager.broadcast` or `manager.send`, asserting zero occurrences. Without this guard, a future change could accidentally re-introduce dual-path broadcasting.

## Mention Fan-Out

### @everyone
`test_send_message_fans_out_everyone_mention_to_all_members` verifies that `@everyone` creates one `MentionNotification` per non-sender member and increments their mention unread counter. The test checks that the sender is excluded from their own notification.

### Deduplication
`test_send_message_user_and_broadcast_mention_dedupes` covers the case where a message contains both `@user(u2)` and `@everyone`. The rule is that `u2` must receive exactly **one** notification, not two. Without the deduplication guard, a user mentioned by name in a broadcast message would get double-notified.

## Unread Event Fan-Out
`test_send_message_emits_unread_update_for_non_senders` verifies that every non-sender group member receives an `unread.update` event so their badge counts refresh in real time. The sender is excluded — they are inherently caught-up with their own message.

## Reply Behavior
Two tests cover the inline-reply model:

- **`test_send_reply_does_not_bump_thread_count`** — replies no longer use threaded conversations; the parent message's `thread_count` must stay unchanged. This guards against a regression where the old thread-counting path is accidentally re-activated.
- **`test_send_reply_emits_message_new_not_thread_reply`** — inline replies fan out as `MessageNew` (same as top-level messages), not as a `ThreadReply` event. This confirms the architectural decision that threads were replaced with inline quotes.

## Test Infrastructure

Tests share the `_fake_group` helper pattern from the group emit tests: a `SimpleNamespace` is constructed with the minimum fields `MessageService` reads, and `_get_group_or_404` is patched to return it. `emit` is replaced with a local recording coroutine.

## Known Gaps

No TODOs or FIXMEs are present. The test for `toggle_reaction` is listed in the AST; if the `MessageReaction` event shape changes, the test assertions should be updated to match.
