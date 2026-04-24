---
{
  "title": "Message Service - Chat Message CRUD, Reactions, Threads, and Search",
  "summary": "The `MessageService` class handles all message lifecycle operations in the chat domain, including sending, editing, deletion, reactions, thread replies, pin management, mention fan-out, and full-text search. It was split from the original `service.py` monolith and extended with agent message creation and workspace-scoped search.",
  "concepts": [
    "MessageService",
    "send message",
    "reactions",
    "thread replies",
    "cursor pagination",
    "mention fan-out",
    "agent messages",
    "full-text search",
    "realtime events",
    "unread counters",
    "soft delete",
    "attachments"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "realtime",
    "search"
  ],
  "source_docs": [
    "b777a35e8b9e006b"
  ],
  "backlinks": null,
  "word_count": 414,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MessageService` is the stateless business logic layer for chat messages. It operates at the intersection of several subsystems: the `Message` and `Group` ODM models, `UnreadService` for bump-on-mention, `NotificationService` for push notifications, and the realtime emit layer for broadcasting events to connected WebSocket clients.

## Refactor History

This module was extracted from the original `service.py` monolith as part of a targeted refactor that separated group and message concerns so each could evolve independently. The refactor also introduced:

- `create_agent_message()` - a static method for agent bridge use, replacing ad-hoc `Message` document creation scattered across the agent layer.
- `search_workspace_messages()` - workspace-scoped search scoped to groups the caller is a member of (Cluster E sub-PR 2).
- Attachment forwarding fix (2026-04-19): `message.sent` events now carry `attachments` metadata so the channel agent path matches the DM path.

## Key Methods

### `send_message(group_id, user_id, body)`

The core send path: verifies the caller can post, inserts the `Message` document, calls `_fan_out_mention` for each `@mention` target asynchronously, emits `message.sent` with attachments, and updates unread counters for group members.

### `_fan_out_mention(target)`

Mention fan-out is a separate async step so a slow notification backend does not block the send response. Each mention triggers `UnreadService.bump_mention` and a push notification via `NotificationService`.

### `create_agent_message(group_id, agent_id, content, attachments)`

Provides a clean API for the agent bridge to insert messages without constructing `Message` documents directly. Centralising the creation contract means schema changes propagate automatically.

### `toggle_reaction(message_id, user_id, emoji)`

Reactions are idempotent toggles: if the user has already reacted with the given emoji, it is removed; otherwise it is added. This prevents duplicate reactions from double-clicks and avoids a separate remove-reaction endpoint.

### `get_messages(group_id, user_id, cursor, limit)`

Returns a cursor-paginated page of messages. The cursor is an ObjectId, which is monotonically ordered by insertion time - no separate `created_at` index is needed.

### Search Methods

`search_messages` is scoped to a single group; `search_workspace_messages` fans out across all groups the caller belongs to. The membership filter prevents information leakage to users who have left a group.

## `_get_group_message_or_404`

This helper loads a message and enforces that it exists, is not soft-deleted, and belongs to a group context. The context-type guard prevents message IDs from one context being used to read or mutate messages in another.

## Known Gaps

- Legacy `Message` rows written before `context_type` was introduced may lack the field in MongoDB. The unread count query works around this by filtering on `group` alone.
- Mention fan-out uses `asyncio.create_task`; failures are logged but not retried.