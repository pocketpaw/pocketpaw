---
{
  "title": "Comment Document — Threaded Comments on Pockets",
  "summary": "Defines the `Comment` document model for threaded comments that can target pockets, widgets, or agents. Supports threading via a parent comment reference, @mentions, and comment resolution for task-like workflows.",
  "concepts": [
    "Comment",
    "CommentTarget",
    "CommentAuthor",
    "threaded comments",
    "denormalization",
    "mentions",
    "resolution",
    "pockets",
    "widgets"
  ],
  "categories": [
    "models",
    "collaboration",
    "comments"
  ],
  "source_docs": [
    "726b71c32f1ea43d"
  ],
  "backlinks": null,
  "word_count": 407,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Comment Document — Threaded Comments on Pockets

`cloud/models/comment.py`

## Purpose

The `Comment` document enables collaborative discussion on PocketPaw's core entities (pockets, widgets, agents). It supports threaded replies, @mentions, and resolution — similar to comment threads in Google Docs or Figma.

## Embedded Models

### CommentTarget

Identifies what the comment is attached to:
- `type: str` — Restricted to `"pocket"`, `"widget"`, or `"agent"` via regex pattern validation
- `pocket_id: str` — Always present (even for widget comments, since widgets live inside pockets)
- `widget_id: str | None` — Set when commenting on a specific widget within a pocket

This design means widget comments are always associated with their parent pocket, enabling queries like "all comments on this pocket and its widgets."

### CommentAuthor

Denormalized author information:
- `id: str` — User ID
- `name: str` — Display name at time of comment
- `avatar: str` — Avatar URL at time of comment

Denormalization means comment display doesn't require joining with the User collection. The trade-off is that if a user changes their name/avatar, old comments show stale information.

## Comment Document

Extends `TimestampedDocument`:

- `workspace: Indexed(str)` — Workspace scoping
- `target: CommentTarget` — What this comment is on
- `thread: str | None` — Parent comment ID for replies (null = top-level comment)
- `author: CommentAuthor` — Denormalized author info
- `body: str` — Comment text
- `mentions: list[str]` — User IDs of mentioned users (for notifications)
- `resolved: bool` — Whether the comment thread is resolved
- `resolved_by: str | None` — User ID who resolved it

### Database Settings
- Collection name: `comments`
- Compound index on `(target.pocket_id, created_at desc)` — Optimizes the common query "all comments on this pocket, newest first"

## Design Decisions

- **Denormalized author**: Avoids N+1 queries when loading comment threads. Stale names are acceptable since comments are historical records.
- **Thread as parent reference**: Simple threading model — replies point to their parent. No nested threads (replies to replies would create a flat list under the original parent).
- **Resolution workflow**: `resolved`/`resolved_by` enables a lightweight task tracking pattern where comments can be "done" without deletion.

## Known Gaps

- The index references `created_at` but the field is actually `createdAt` (from TimestampedDocument). This index may not work as intended.
- No cascade delete — deleting a pocket doesn't automatically delete its comments.
- `mentions` stores user IDs but there's no validation that the mentioned users exist or belong to the workspace.
