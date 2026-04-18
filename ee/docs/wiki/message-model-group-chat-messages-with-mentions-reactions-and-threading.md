---
{
  "title": "Message Model: Group Chat Messages with Mentions, Reactions, and Threading",
  "summary": "Beanie document model for chat messages within groups. Supports @mentions (users, agents, everyone), file/image/pocket attachments, emoji reactions, threaded replies, soft deletion, and edit tracking.",
  "concepts": [
    "Message",
    "Mention",
    "Attachment",
    "Reaction",
    "threading",
    "soft delete",
    "compound index",
    "chat",
    "group messaging"
  ],
  "categories": [
    "Models",
    "Messaging",
    "Data Layer"
  ],
  "source_docs": [
    "f6a5c25b1ad10666"
  ],
  "backlinks": null,
  "word_count": 485,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Message Model: Group Chat Messages with Mentions, Reactions, and Threading

## Purpose

The `Message` model is the core messaging primitive in PocketPaw Enterprise. Each message belongs to a group and supports rich features: @mentions that can target users or AI agents, attachments of various types (files, images, pockets, widgets), emoji reactions, and threaded replies.

## Design Decisions

### Compound Index for Chronological Queries
The index `[("group", 1), ("createdAt", -1)]` is optimized for the most common query pattern: "get the latest messages in a group." The descending `createdAt` order means MongoDB can satisfy `find(group=X).sort(createdAt=-1).limit(50)` using an index scan without a sort stage.

### Dual Sender Identity
Messages track both `sender` (user ID) and `sender_type` (`user` or `agent`). When `sender_type` is `agent`, the `agent` field carries the agent ID. This dual-tracking exists because system messages (like "User joined the group") have `sender = None`, and the frontend needs to distinguish between human messages, agent messages, and system notifications for rendering.

### Mention Types
The `Mention` model supports `user`, `agent`, and `everyone` types. This is important for agent response modes — when a group agent is in `mention_only` mode, it needs to check whether any `Mention` in the message targets it by agent ID.

### Soft Delete
The `deleted` boolean flag enables soft deletion rather than hard deletion. This preserves message threading integrity — if a parent message is hard-deleted, all reply references would become dangling. Soft delete also enables "This message was deleted" UI placeholders.

### Attachment Polymorphism
The `Attachment` model uses a `type` discriminator (`file`, `image`, `pocket`, `widget`) with a generic `meta` dict for type-specific data. This is a flexible-schema pattern — adding a new attachment type only requires a new `type` value and appropriate `meta` keys, no schema migration.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `group` | `Indexed(str)` | Parent group ID |
| `sender` | `str | None` | User ID (null for system messages) |
| `sender_type` | `str` | `user` or `agent` |
| `agent` | `str | None` | Agent ID when sender is an agent |
| `content` | `str` | Message text |
| `mentions` | `list[Mention]` | @mention targets |
| `reply_to` | `str | None` | Parent message ID for threading |
| `attachments` | `list[Attachment]` | Files, images, pockets, widgets |
| `reactions` | `list[Reaction]` | Emoji reactions with user lists |
| `edited` | `bool` | Whether message was edited |
| `deleted` | `bool` | Soft delete flag |

## Known Gaps

- No `deleted_at` timestamp — soft-deleted messages cannot be filtered by deletion time.
- The `Reaction` model stores user IDs in a list, which does not scale well for messages with many reactions (e.g., hundreds of users reacting with the same emoji).
- No message length limit enforced at the model level.
- Thread depth is unlimited — deeply nested threads could cause UI rendering issues.
