---
{
  "title": "Comment Document — Threaded Comments on Pockets and Widgets",
  "summary": "The Beanie ODM document for threaded comments attached to pockets, widgets, or agents. Supports reply threading via a `thread` parent ID, user mentions, and resolution tracking for review-style workflows.",
  "concepts": [
    "Comment document",
    "Beanie ODM",
    "CommentTarget",
    "CommentAuthor",
    "threaded comments",
    "denormalized identity",
    "mentions",
    "resolution tracking",
    "pocket",
    "widget",
    "threading model"
  ],
  "categories": [
    "data modeling",
    "MongoDB",
    "collaboration"
  ],
  "source_docs": [
    "9b48177b3b87a49c"
  ],
  "backlinks": null,
  "word_count": 510,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`comment.py` defines the `Comment` Beanie document and two embedded Pydantic models: `CommentTarget` (what the comment is attached to) and `CommentAuthor` (a denormalized snapshot of the author's identity at the time of posting).

## CommentTarget

```python
class CommentTarget(BaseModel):
    type: str = Field(pattern="^(pocket|widget|agent)$")
    pocket_id: str
    widget_id: str | None = None
```

Every comment is attached to a pocket. Widget-level comments additionally set `widget_id` to specify which widget within the pocket the comment refers to. Agent-type targets allow comments on an agent's configuration page, using `pocket_id` to reference the pocket the agent is displayed within.

The `type` field uses a regex pattern validator rather than a `Literal` type for forward compatibility — adding a new comment target type requires only changing the regex, not updating an enum used across the codebase.

## CommentAuthor — Denormalized Identity

```python
class CommentAuthor(BaseModel):
    id: str
    name: str
    avatar: str = ""
```

The author's name and avatar are stored inside the comment document rather than being joined from the `users` collection at read time. This denormalization is intentional: it ensures comments render correctly even if the author's account is deleted or their display name changes. Historical comments preserve the identity of their author at the time they were written, which matters for audit and collaboration contexts.

## Threading Model

```python
thread: str | None = None  # Parent comment ID for replies
```

The threading model is shallow — a `thread` value of `None` means the comment is a top-level post; any non-null value is the ID of the parent comment. The schema does not enforce a maximum thread depth at the database level, but the API layer and UI treat comments as one level deep (a reply cannot itself have replies). This keeps the data model simple while supporting the most common collaboration pattern.

## Mentions and Resolution

```python
mentions: list[str] = Field(default_factory=list)  # User IDs
resolved: bool = False
resolved_by: str | None = None
```

`mentions` stores a list of user IDs. This enables the notification system to fan out mention alerts without re-parsing the comment body. `resolved` / `resolved_by` support the review workflow — a comment thread can be marked as addressed, with the resolver's ID recorded for accountability.

## Index Design

```python
indexes = [
    [("target.pocket_id", 1), ("created_at", -1)],
]
```

The index on `(target.pocket_id, created_at DESC)` covers the dominant read pattern: fetch all comments for a pocket in reverse chronological order. The `created_at` field name in the index uses snake_case (note: `TimestampedDocument` uses camelCase `createdAt`) — this may be a latent inconsistency between the index definition and the actual field name.

## Known Gaps

- The index definition references `created_at` but `TimestampedDocument` stores the field as `createdAt`. If MongoDB does not find a matching field, the index would be on a non-existent path and provide no benefit.
- No index on `thread` — fetching all replies to a parent comment requires a collection scan filtered by `thread == parent_id`.
- Resolution status is stored per-comment but not per-thread. Resolving a top-level comment does not automatically resolve its replies.