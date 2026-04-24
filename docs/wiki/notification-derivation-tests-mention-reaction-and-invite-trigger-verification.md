---
{
  "title": "Notification Derivation Tests: Mention, Reaction, and Invite Trigger Verification",
  "summary": "These tests verify the three primary notification derivation paths — @mention in a group message, emoji reaction, and workspace invite — correctly call NotificationService.create with the right arguments. They also verify that self-targeted events and reaction removals never produce notifications.",
  "concepts": [
    "NotificationService",
    "derivation tests",
    "mention notification",
    "reaction notification",
    "invite notification",
    "self-notify suppression",
    "spy pattern",
    "AsyncMock",
    "MessageService",
    "WorkspaceService",
    "source reference"
  ],
  "categories": [
    "testing",
    "notifications",
    "event derivation",
    "test"
  ],
  "source_docs": [
    "e5bec7be14a9e884"
  ],
  "backlinks": null,
  "word_count": 348,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Notifications in PocketPaw are derived from domain events rather than being explicitly created by API callers. When a user sends a message with a mention, adds a reaction, or creates an invite, the service layer detects the social event and calls `NotificationService.create`. These tests prove that contract holds and that the no-self-notify rule is enforced.

## Testing Strategy: Spy on the Derivation Call

Each test patches everything up to `NotificationService.create` and inspects `await_args`:

```python
spy = AsyncMock()
with patch("ee.cloud.chat.message_service.NotificationService.create", new=spy):
    await MessageService.send_message(...)
spy.assert_awaited_once()
kwargs = spy.await_args.kwargs
assert kwargs["recipient"] == "u2"
assert kwargs["kind"] == "mention"
```

This approach tests derivation logic — branching and argument assembly — without exercising the persistence layer or the real-time emit path. Those are covered separately in `test_service.py`.

## Mention Derivation

```python
assert kwargs["recipient"] == "u2"
assert kwargs["kind"] == "mention"
assert "#general" in kwargs["title"]
assert kwargs["source"].type == "message"
assert kwargs["source"].id == "m1"
```

The `source` field is a typed reference (type + id) that lets the frontend deep-link the notification to the originating message. The channel name (`#general`) is included in the title so the recipient knows where the mention occurred.

## Self-Mention Suppression

```python
async def test_send_message_self_mention_does_not_notify():
    # Sender "u1" mentions "u1" -> spy must not be called
    spy.assert_not_awaited()
```

Without self-mention suppression, typing a self-mention would create a notification about your own content. The service compares `sender_id == recipient_id` before calling `NotificationService.create`.

## Reaction Derivation and No-Notify Cases

```python
# u2 reacts to u1's message -> u1 is notified
assert kwargs["recipient"] == "u1"
assert kwargs["kind"] == "reaction"
assert kwargs["body"] == "hey"  # original message content
```

Two additional tests confirm no-notification cases: self-reaction (reactor is also message sender) and reaction removal (toggling an existing reaction).

## Invite Derivation

Invite notifications only fire when the invitee's email is already registered as a user. If the email is unknown, no notification is created (an email invite is sent instead).

```python
assert kwargs["recipient"] == "u2"
assert kwargs["kind"] == "invite"
assert "Acme" in kwargs["title"]
assert kwargs["source"].type == "invite"
```

## Known Gaps

None identified. All three derivation paths and their no-notify conditions are covered.