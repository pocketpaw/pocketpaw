---
{
  "title": "Chat Session Document for Pocket and Group Contexts",
  "summary": "The `Session` document stores chat session metadata shared between one-on-one pocket conversations and multi-user group chats, using a `context_type` discriminator validated on construction. It tracks message counts, last activity, and soft deletion, with four compound indexes covering the dominant query patterns.",
  "concepts": [
    "Session document",
    "context_type discriminator",
    "sessionId",
    "group chat",
    "pocket session",
    "soft delete",
    "model validator",
    "camelCase aliases",
    "compound indexes",
    "multi-tenant"
  ],
  "categories": [
    "data-models",
    "messaging",
    "agent-memory",
    "enterprise-cloud"
  ],
  "source_docs": [
    "b3025c098b9884cc"
  ],
  "backlinks": null,
  "word_count": 519,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`Session` is the metadata envelope for a chat interaction. It does not store messages (those live in `Message`) but records the session identity (`sessionId`), ownership, binding (to a pocket or group), activity timestamps, and message count. Both the pocket agent-memory system and the group chat system share this one document type.

## Why Unify Pocket and Group Sessions?

Session-level operations — listing a user's recent conversations, showing last-activity indicators, enforcing soft deletion — are identical regardless of whether the session is a 1-on-1 with an agent or a group discussion. A unified model means one query covers both types for the "recent chats" sidebar, rather than two queries that must be merged and re-sorted.

## `sessionId` vs. `_id`

The document carries both `_id` (MongoDB ObjectId, inherited from Beanie) and a separate `sessionId` string. The `sessionId` is the application-layer identifier used in API routes, the LLM context builder, and the frontend. This decoupling means the same session can be referenced by a stable string key without exposing raw ObjectIds in URLs, and the `sessionId` can be generated with a format that carries semantic meaning (e.g., a UUID with a user prefix).

## Context Discrimination

Like the `Message` model, `Session` uses a `context_type: Literal["pocket", "group"] | None` field with a `_enforce_context` model validator. The optional type accommodates legacy constructors during the rewrite window — the validator infers the type from field presence:

- If `group` is set → `"group"`
- Otherwise → `"pocket"` (covers pocket sessions and "pocket-less" sessions that bind only to a `sessionId`)

Once inferred, hard constraints fire:

- Pocket sessions must not carry a `group`.
- Group sessions must carry a `group` and must not carry a `pocket`.

This prevents sessions from being bound to both a pocket and a group simultaneously, which would make the message routing logic ambiguous.

## camelCase Aliases

`sessionId`, `lastActivity`, and `messageCount` use camelCase aliases matching the frontend JSON contract. `model_config = {"populate_by_name": True}` allows Python code to use snake_case internally while the serialized API response uses camelCase, avoiding a translation layer in the router.

## Soft Deletion

`deleted_at: datetime | None = None` enables soft delete. Deleted sessions remain queryable for audit purposes but are excluded from active listings. The absence of a hard-delete path means conversation history is preserved even when users "delete" a chat.

## Index Strategy

Four compound indexes:

1. `(workspace, context_type, lastActivity DESC)` — list all sessions of a given type across the workspace, newest first.
2. `(workspace, pocket, lastActivity DESC)` — list sessions for a specific pocket.
3. `(workspace, group, agent)` — look up the active agent session for a group.
4. `(workspace, owner, lastActivity DESC)` — list a user's own sessions.

All indexes lead with `workspace`, ensuring multi-tenant query isolation at the index level.

## Known Gaps

- `deleted_at` has no index — queries that filter out deleted sessions (`deleted_at=None`) will not benefit from an index-level filter.
- `messageCount` is incremented by the service layer and could drift if messages are bulk-deleted without a counter correction.
- `context_type` being optional means the validator is the sole integrity gate for the constraint between `pocket`/`group` fields.