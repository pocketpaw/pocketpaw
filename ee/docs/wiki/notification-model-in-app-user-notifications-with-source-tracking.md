---
{
  "title": "Notification Model: In-App User Notifications with Source Tracking",
  "summary": "Beanie document model for in-app notifications delivered to users. Supports multiple notification types (mentions, replies, invites, agent completions), read/unread state, optional expiry, and a polymorphic source reference.",
  "concepts": [
    "Notification",
    "NotificationSource",
    "in-app notifications",
    "read state",
    "compound index",
    "polymorphic reference"
  ],
  "categories": [
    "Models",
    "Notifications",
    "Data Layer"
  ],
  "source_docs": [
    "ae8fbce33e64fab4"
  ],
  "backlinks": null,
  "word_count": 351,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Notification Model: In-App User Notifications with Source Tracking

## Purpose

The `Notification` model powers the in-app notification system. Each notification targets a specific user within a workspace and can originate from various sources (messages, pockets, invites, agent actions). The model is designed for fast retrieval of unread notifications per user.

## Design Decisions

### Compound Index for Notification Feed
The index `[("recipient", 1), ("read", 1), ("created_at", -1)]` is tuned for the primary query: "get this user's unread notifications, newest first." By including `read` in the index, MongoDB can skip already-read notifications during index scanning rather than filtering them post-scan.

### Polymorphic Source
The `NotificationSource` model uses `type` + `id` + optional `pocket_id` to reference the origin of a notification. This avoids a rigid foreign key structure — a notification might come from a message, a pocket share, an invite, or an agent completion. The `pocket_id` field exists because some notification types (like agent completions) need to link back to a pocket context even when the primary source is something else.

### Notification Types
The `type` field supports: `mention`, `comment`, `reply`, `invite`, `agent_complete`, `pocket_shared`. These are not enum-constrained at the model level, allowing new types to be added without a schema migration.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `workspace` | `Indexed(str)` | Workspace scope |
| `recipient` | `Indexed(str)` | Target user ID |
| `type` | `str` | Notification category |
| `title` | `str` | Display title |
| `body` | `str` | Display body |
| `source` | `NotificationSource | None` | Origin reference |
| `read` | `bool` | Read/unread state |
| `expires_at` | `datetime | None` | Optional TTL |

## Known Gaps

- The `expires_at` field exists but there is no TTL index defined to auto-delete expired notifications. A background task or MongoDB TTL index would need to be set up separately.
- No batch "mark all as read" optimization visible — marking many notifications read would require individual document updates.
- The `type` field is a free-form string with no validation constraint, unlike other models that use `Field(pattern=...)`.
