---
{
  "title": "ReadState: Per-User Unread Message Tracking",
  "summary": "The `ReadState` document records the last message a user has read in each group, enabling O(1) unread count computation by comparing a single pointer against the group's `message_count` counter. It is updated on `read.ack` WebSocket events and tracks unread mention counts separately.",
  "concepts": [
    "ReadState",
    "unread count",
    "counter pattern",
    "message cursor",
    "mention tracking",
    "unique index",
    "upsert",
    "WebSocket ack",
    "O(1) unread computation",
    "group chat"
  ],
  "categories": [
    "data-models",
    "messaging",
    "performance",
    "enterprise-cloud"
  ],
  "source_docs": [
    "7830976307dc9560"
  ],
  "backlinks": null,
  "word_count": 454,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ReadState` solves a classic messaging infrastructure problem: how do you efficiently show users how many unread messages are in each group without querying and counting all messages newer than their last read?

## The Counter Pattern

The standard naive approach — `SELECT COUNT(*) FROM messages WHERE group=? AND created_at > last_read_at` — requires a database read for every group on every page load. For a workspace with 50 groups, that is 50 count queries per user per load.

PocketPaw avoids this by maintaining two counters:

1. `Group.message_count` — incremented atomically on every new message.
2. `ReadState.last_read_message_id` — the ID of the last message the user acknowledged.

At read time, the service compares `Group.message_count` against the position represented by `last_read_message_id`. The unread delta is the difference, computable without scanning any message documents.

## Why `last_read_message_id` Instead of `last_read_at`?

Storing a message ID rather than a timestamp prevents two failure modes:

1. **Clock skew** — if two messages arrive within the same millisecond (possible on high-throughput groups or in tests), a timestamp cursor would ambiguously include or exclude one of them depending on comparison operator. A message ID is unambiguous.
2. **Reordering** — messages edited after the fact change their `updated_at` but not their `_id`. A timestamp cursor against `updated_at` would incorrectly count edited old messages as new.

## `mention_unread` Counter

The separate `mention_unread` integer tracks how many unread messages contain an `@mention` of this user. Mentions have higher notification priority than regular messages — clients typically show a distinct badge for mentions. Tracking it separately avoids scanning mention fields on all unread messages.

## Unique Index on `(user, group)`

The `IndexModel([("user", ASCENDING), ("group", ASCENDING)], unique=True)` constraint ensures exactly one `ReadState` document exists per user-group pair. This is an idempotency guard for the upsert pattern: the `read.ack` handler can call `update_one(..., upsert=True)` without worrying about creating duplicate rows under concurrent acks from multiple browser tabs.

## Update Trigger: `read.ack` WebSocket Events

The `ReadState` is written on `read.ack` events — messages sent by the client over WebSocket when the user scrolls a group channel into view. This event-driven approach means the server does not need to poll or infer read state; the client declares it explicitly. The tradeoff is that a client that crashes mid-session may leave `ReadState` pointing to a message several seconds older than the true last-read position.

## Known Gaps

- No index on `(group)` alone — bulk operations for a group (e.g., resetting all read states when a group is deleted) require a collection scan.
- `mention_unread` is not decremented when a mentioned message is deleted — it can over-count after soft deletes.
- The counter-based unread computation assumes `message_count` is strictly monotonic; out-of-order message delivery could theoretically desync the counter.