---
{
  "title": "MemoryFactDoc — Beanie Document for Agent Long-Term and Daily Memory",
  "summary": "The `MemoryFactDoc` Beanie ODM document stores agent long-term and daily memory facts in the `memory_facts` MongoDB collection. It is distinct from the `messages` collection and carries workspace and user ownership fields to support multi-tenant scoping.",
  "concepts": [
    "MemoryFactDoc",
    "Beanie ODM",
    "memory_facts collection",
    "long-term memory",
    "daily memory",
    "workspace scoping",
    "user_id",
    "MongoDB indexes",
    "TimestampedDocument",
    "multi-tenant",
    "memory tiers"
  ],
  "categories": [
    "memory",
    "MongoDB",
    "data modeling",
    "multi-tenancy"
  ],
  "source_docs": [
    "d5511bc2c41aec57"
  ],
  "backlinks": null,
  "word_count": 437,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`documents.py` defines `MemoryFactDoc`, the MongoDB document schema for agent persistent memory entries. It stores two memory tiers: `"long_term"` facts that persist indefinitely and `"daily"` summaries that capture session-level context.

## Why a Separate Collection from Messages

Agent memory facts (`memory_facts`) and chat messages (`messages`) serve fundamentally different product surfaces and access patterns:

- `messages` are chat-UI-facing — they are paginated by session, rendered to users, and frequently queried in recency order within a session.
- `memory_facts` are AI-runtime-facing — they are retrieved by the memory manager to construct agent context, queried by type and recency across all sessions for a user, and are not directly displayed in the UI.

Separating them avoids polluting the chat message stream with memory management records and allows independent index optimization.

## Schema

```python
class MemoryFactDoc(TimestampedDocument):
    type: Indexed(str)  # "long_term" or "daily"
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    user_id: str | None = None
    workspace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = "memory_facts"
        indexes = [
            [("type", 1), ("workspace_id", 1), ("user_id", 1), ("createdAt", -1)],
            [("workspace_id", 1), ("createdAt", -1)],
            "tags",
        ]
```

## Multi-Tenant Scoping

`workspace_id` and `user_id` are both nullable. When `workspace_id` is `None`, the row represents an OSS/single-tenant usage. Cloud API routes that read `memory_facts` always filter by `workspace_id` to prevent cross-tenant reads. The nullable design allows the same collection to serve both deployment models without schema migration when upgrading from OSS to cloud.

The `user_id` field mirrors how the OSS `FileMemoryStore` partitions long-term memory — it uses the user's identity as the ownership key, so the same retrieval semantics apply across both storage backends.

## Index Design

The composite index `(type, workspace_id, user_id, createdAt DESC)` covers the most common query pattern: fetch the most recent N long-term or daily facts for a specific user in a specific workspace. The secondary `(workspace_id, createdAt DESC)` index covers workspace-level reporting queries. The `tags` index supports tag-based filtering for agents that use tag metadata to categorize facts.

## Inheritance from TimestampedDocument

Inheriting from `TimestampedDocument` means `createdAt` and `updatedAt` are automatically managed. The `before_event(Insert)` and `before_event(Replace, Save, Update)` hooks ensure these fields are never stale regardless of how the document is saved.

## Known Gaps

- There is no TTL index on `daily` facts. Daily summaries are intended to be ephemeral (session-level) but will accumulate indefinitely without a scheduled cleanup job or TTL configuration.
- The `tags` field is a plain list with no uniqueness constraint or controlled vocabulary — tag drift across agents could make tag-based filtering unreliable over time.
- `metadata` is an unconstrained dict, which makes schema evolution harder to track.