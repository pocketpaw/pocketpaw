---
{
  "title": "Unread Service - Per-User Unread Counts and Mention Badges",
  "summary": "`UnreadService` tracks per-user, per-group unread message counts and mention badges by combining a `ReadState` watermark document with a live message count query. It provides the data for inbox badges and the visual unread indicator in the sidebar.",
  "concepts": [
    "UnreadService",
    "ReadState",
    "unread count",
    "mention badge",
    "ObjectId cursor",
    "mark read",
    "bump mention",
    "MongoDB",
    "Beanie",
    "N+1 query",
    "watermark"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "notifications",
    "MongoDB"
  ],
  "source_docs": [
    "ca685d429b8f7e12"
  ],
  "backlinks": null,
  "word_count": 392,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Knowing how many unread messages a user has in each group is a deceptively complex problem. `UnreadService` solves it with a two-part model:

1. **`ReadState` watermark** - a document per (user, group) pair storing `last_read_message_id` and a `mention_unread` integer counter.
2. **Live count query** - on each `list_unreads` call, the service queries messages with `_id > last_read_message_id` for each group the user belongs to.

## ObjectId as a Cursor

MongoDB ObjectIds embed a 4-byte Unix timestamp in their most significant bytes, so `$gt` on `_id` is equivalent to 'messages created after this one' - no separate `created_at` index is required. This is both a performance win (the primary index is used) and a correctness guarantee (ObjectId ordering within a single MongoDB instance is insertion-ordered, unlike wall clocks which can skew).

## Legacy Schema Compatibility

The count query filters on `group` alone, not on `context_type`. This is a deliberate defensive choice: rows written before `context_type` was added to the schema have the field absent in MongoDB. A strict `context_type='group'` equality filter would silently under-count unreads for legacy messages. The service trades a small theoretical risk for correctness on old data.

## `mark_read(user_id, group_id, last_message_id)`

Called when the client acknowledges reading up to a given message. It upserts the `ReadState` document and clears `mention_unread`. The act of opening a channel implicitly dismisses mention badges - a user who opens the channel has seen the mentions.

## `bump_mention(user_id, group_id)`

Increments `mention_unread` without touching `last_read_message_id`. Called by `MessageService._fan_out_mention` when a message contains an `@user` mention. The increment is a MongoDB `$inc` operation, making it atomic - concurrent mentions do not lose counts.

## `list_unreads(user_id, workspace_id)`

The main read path fetches all non-archived groups the user is a member of, then for each group loads the `ReadState` (if any) and counts messages after the watermark. Returns `[{group_id, unread, mention_unread}]`.

The N+1 query pattern here (one count query per group) is a known trade-off. For users with many groups this can be slow; a future optimisation could batch counts into a single aggregation pipeline.

## Known Gaps

- `list_unreads` issues one count query per group - O(N) in the number of joined groups. A single aggregation `$facet` query would be more efficient.
- `mark_read` clears `mention_unread` unconditionally when the group is opened, even if the user opened it from a scroll position above the unread messages.