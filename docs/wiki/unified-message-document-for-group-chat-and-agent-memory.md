---
{
  "title": "Unified Message Document for Group Chat and Agent Memory",
  "summary": "The `Message` model stores two structurally different record types — multi-user group chat rows and single-agent LLM memory rows — in one MongoDB collection, discriminated by a `context_type` field. A model validator enforces strict field invariants per context type, preventing cross-contamination of group chat metadata into agent history and vice versa.",
  "concepts": [
    "unified message store",
    "context_type discriminator",
    "group chat",
    "pocket agent memory",
    "LLM history",
    "model validator",
    "Beanie TimestampedDocument",
    "mentions",
    "reactions",
    "attachments",
    "multi-tenant",
    "MongoDB indexes"
  ],
  "categories": [
    "data-models",
    "messaging",
    "agent-memory",
    "enterprise-cloud"
  ],
  "source_docs": [
    "28e074d9848eea80"
  ],
  "backlinks": null,
  "word_count": 563,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`Message` is PocketPaw's unified message store. It serves two very different consumers from a single Beanie `TimestampedDocument` subclass: the group chat system (multi-user, threaded, with reactions and mentions) and the pocket agent memory system (LLM conversation history in user/assistant/system roles).

## Why a Single Collection?

Separate collections would force every caller that needs to reason about "all messages" — search, export, analytics, retention enforcement — to query two collections and merge results. A single collection with a discriminator field lets the storage layer apply one retention policy, one backup, and one index strategy. The `context_type` field acts as the discriminator; the composite indexes are tuned per type.

## Supporting Models

**`Mention`** — Captures `@user`, `@agent`, and `@everyone` references within message content. The `id` and `display_name` fields allow the frontend to hyperlink mentions without a secondary lookup. The `type` field (defaulting to `"user"`) disambiguates agent mentions from user mentions, which matters for notification routing.

**`Attachment`** — A flexible envelope for files, images, pockets, and widgets. The `meta` dict absorbs type-specific metadata (MIME type, dimensions, widget config diff) without requiring schema changes per attachment type.

**`Reaction`** — Groups emoji reactions by emoji string, listing all reactor user IDs. This avoids one document per reaction and keeps the full reaction summary inside the message document for O(1) render.

## Context Discrimination and the `_enforce_context` Validator

The `context_type` field is intentionally optional at the schema level. This backward-compatibility choice allows legacy code that sets `group=...` and `sender=...` without an explicit `context_type` to still produce valid documents during the rewrite window. The `_enforce_context` model validator infers the type when absent:

- If `session_key` or `role` is present → infer `"pocket"`
- Otherwise → infer `"group"`

After inference, the validator enforces hard invariants:

- Group messages must have `group` set; must not carry `session_key` or `role`.
- Pocket messages must have `session_key`; `role` must be one of `user/assistant/system`; must not carry `group`, `mentions`, `reactions`, or `reply_to`.

These rejections prevent subtle bugs where a pocket message accidentally carries group-chat fields that would be silently ignored by the LLM context builder but would bloat documents and confuse analytics.

## Workspace Scoping for Multi-Tenant Deployments

The `workspace_id` field is stamped on every row. For group rows, callers populate it from the group's workspace. For pocket rows, the adapter resolves it from the linked `Session.workspace` at write time. This design means multi-tenant EE deployments can scope all reads at the adapter layer with a single `workspace_id` filter rather than joining through sessions or groups.

## Index Strategy

Four compound indexes cover the dominant query patterns:

1. `(context_type, group, createdAt DESC)` — group chat timeline queries
2. `(workspace_id, session_key, createdAt ASC)` — pocket LLM history by session within a tenant
3. `(session_key, createdAt ASC)` — pocket history without workspace filter (single-tenant path)
4. `(group, createdAt DESC)` — group messages without context_type filter (legacy path)

The duplicate `session_key` index (3 vs 2) exists to support old query paths that did not include `workspace_id`; this is a migration artifact.

## Known Gaps

- `context_type` being optional means the validator is load-bearing for data integrity; if bypassed (e.g., direct MongoDB writes), malformed rows will not be caught until read time.
- No TTL index: pocket memory rows accumulate indefinitely unless the caller explicitly purges old sessions.
- The soft-delete `deleted` flag on group messages has no index; queries filtering `deleted=False` will scan all group messages.