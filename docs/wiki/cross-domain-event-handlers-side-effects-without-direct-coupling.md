---
{
  "title": "Cross-Domain Event Handlers: Side Effects Without Direct Coupling",
  "summary": "This module wires up application-level event handlers that react to events from other domains — such as invite acceptance, message delivery, pocket sharing, and member removal — without importing those domains directly. It is registered once at app startup and acts as the glue layer for cross-cutting side effects.",
  "concepts": [
    "event bus",
    "pub/sub",
    "cross-domain side effects",
    "invite.accepted",
    "message.sent",
    "pocket.shared",
    "member.removed",
    "notification creation",
    "group membership",
    "startup registration"
  ],
  "categories": [
    "event handling",
    "cloud EE",
    "domain architecture"
  ],
  "source_docs": [
    "6f48a6fceee3e0b8"
  ],
  "backlinks": null,
  "word_count": 418,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/shared/event_handlers.py` is the registration point for side effects that span domain boundaries. Rather than having the invite domain import group logic, or the messaging domain import notification logic, each domain emits an event and this module handles the downstream consequences.

## Why Not Direct Calls

Direct imports across domain boundaries create circular dependency chains and tightly couple unrelated subsystems. If `invite.accepted` handling were inlined into the invite service, that service would need to know about groups, notifications, and workspace membership — unrelated concerns that should evolve independently. The event bus pattern keeps each domain focused: `invite` emits `invite.accepted`, and this module observes it.

## Registered Handlers

### _on_invite_accepted
When a user accepts an invite that includes a `group_id`, the handler adds the user to that group automatically. The guard `if user_id not in group.members` prevents duplicate adds if the event fires more than once. A notification is also created to confirm the action.

### _on_message_sent
Handles fan-out side effects of a new message: creating notifications for mentioned users and updating group-level statistics (message count, last activity timestamp). Centralizing this prevents every message-sending path from independently managing mention notifications.

### _on_pocket_shared
When an agent or user shares a pocket with another user, this handler creates an in-app notification. Without it, pocket shares would be silently delivered with no user-visible feedback.

### _on_member_removed
When a member is removed from a workspace, this handler cleans up their group memberships. Without this cleanup, removed members would remain in group `members` arrays, potentially receiving messages and notifications after their workspace access was revoked.

## Registration Pattern

`register_event_handlers()` is called once at app startup. All handlers are private (`_on_*`). Only `register_event_handlers` is exported, enforcing that the event bus subscriptions are always done through the registration function rather than scattered across the codebase.

## Error Isolation

Each handler wraps its database operations in `try/except` blocks and logs failures with `logger.exception`. An error in one handler — for example, failing to find a group on invite acceptance — does not propagate back to the event emitter or abort other registered handlers for the same event.

## Known Gaps

- The `_on_message_sent` handler runs synchronously within the event emit cycle for group stat updates. High-volume channels could create a backlog if stat writes become slow. Background task dispatch (as used in the agent bridge) would decouple this.
- There is no dead-letter or retry mechanism for failed handler executions. A transient MongoDB timeout in `_on_member_removed` would leave stale group memberships until the next manual cleanup.