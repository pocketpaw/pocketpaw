---
{
  "title": "Cross-Domain Event Handlers for Cloud Side Effects",
  "summary": "Registers handlers for domain events (invite.accepted, message.sent, pocket.shared, member.removed) that trigger cross-domain side effects like auto-adding users to groups, creating notifications, and cleaning up memberships on workspace removal.",
  "concepts": [
    "event handlers",
    "cross-domain side effects",
    "notifications",
    "group auto-join",
    "mention notifications",
    "membership cleanup",
    "event bus subscription"
  ],
  "categories": [
    "cloud",
    "shared",
    "event handling",
    "notifications",
    "domain events"
  ],
  "source_docs": [
    "23b24aa3d758ad7d"
  ],
  "backlinks": null,
  "word_count": 396,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cross-Domain Event Handlers for Cloud Side Effects

## Purpose

When a domain action has consequences in other domains, this module handles those side effects through event subscriptions rather than direct coupling. For example, accepting an invite should add the user to a group and create a notification — but the invite domain shouldn't need to know about groups or notifications.

## Event Handlers

### invite.accepted

When a user accepts a workspace invite that includes a `group_id`:
1. Loads the group and appends the user to `members` (with a duplicate check)
2. Creates an "Invite accepted" notification for the user

This ensures invited users automatically appear in their intended chat groups without the invite service needing to import group models.

### message.sent

Two side effects on every message:
1. **Group stats update** — increments `message_count` and updates `last_message_at` on the Group document
2. **Mention notifications** — creates a notification for each @mentioned user (excluding the sender, to avoid self-notifications)

Mention notifications truncate the message body to 100 characters for the notification preview.

### pocket.shared

Creates a notification when a pocket is shared with a user. The notification includes a source reference back to the pocket for deep linking.

### member.removed

Cleanup when a user is removed from a workspace:
1. Removes the user from `members` of every group in that workspace
2. Removes the user from `shared_with` on every pocket in that workspace

This prevents orphaned access — without it, removed users would still appear in group member lists and retain pocket access.

## Notification Helper

`_create_notification()` is a shared helper that constructs and inserts `Notification` documents. It supports optional `source` metadata (type, id, pocket_id) for deep linking from the notification to its origin.

All notification creation is wrapped in try/except to prevent notification failures from breaking the primary event flow.

## Registration

`register_event_handlers()` subscribes all four handlers to the event bus. Called during app startup alongside `register_agent_bridge()`.

## Known Gaps

- Group stats update (`message_count`, `last_message_at`) has no concurrency protection — simultaneous messages could cause lost updates
- Member removal iterates all groups and pockets in the workspace with individual saves — no bulk update, could be slow for large workspaces
- Notification body for invite acceptance is a hardcoded string `"You joined workspace"` — doesn't include the workspace name
- No event for notification creation itself — other systems can't react to new notifications
