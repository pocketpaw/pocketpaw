---
{
  "title": "Session Model: Pocket-Scoped Chat Sessions with camelCase Frontend Contract",
  "summary": "Beanie document model for chat sessions scoped to pockets, groups, or agents. Sessions track metadata (title, message count, last activity) in MongoDB while actual message content is stored separately in the Python runtime. Uses camelCase field aliases to match the frontend API contract.",
  "concepts": [
    "Session",
    "chat session",
    "camelCase alias",
    "soft delete",
    "pocket-scoped",
    "compound index",
    "denormalized counter",
    "Beanie"
  ],
  "categories": [
    "Models",
    "Sessions",
    "Data Layer",
    "Messaging"
  ],
  "source_docs": [
    "356979315146a236"
  ],
  "backlinks": null,
  "word_count": 468,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Session Model: Pocket-Scoped Chat Sessions with camelCase Frontend Contract

## Purpose

The `Session` model tracks chat session metadata in MongoDB. It is the bridge between the frontend's session management and the backend's message processing. Critically, actual message content is NOT stored in this document — messages are stored elsewhere (in the Python runtime or a separate store). The session is a lightweight pointer with activity metadata.

## Design Decisions

### Unique Session ID
The `sessionId` field is `Indexed(str, unique=True)` — this is a unique business identifier separate from MongoDB's `_id`. The frontend generates session IDs (likely UUIDs) and uses them as the primary reference. The uniqueness constraint prevents duplicate sessions from being created if the frontend retries a create request.

### Hybrid Scoping (Pocket + Group + Agent)
A session can be scoped to a pocket, a group, or an agent — all optional. This flexibility exists because:
- **Pocket sessions**: Chat within a pocket workspace context
- **Group sessions**: Direct messaging within a group channel
- **Agent sessions**: One-on-one conversations with an AI agent
The compound index `[("workspace", 1), ("pocket", 1), ("lastActivity", -1)]` optimizes the most common query: listing a pocket's sessions by recency.

### camelCase Aliases
Fields like `sessionId`, `lastActivity`, and `messageCount` use Pydantic aliases to match the frontend camelCase contract. The `populate_by_name=True` config allows both Python snake_case and JavaScript camelCase access. This pattern is consistent across the codebase (see also Pocket and Widget models).

### Soft Delete via deleted_at
The `deleted_at` timestamp enables soft deletion with a precise deletion time, unlike the boolean `deleted` flag used by Message. This allows time-based cleanup queries (e.g., "purge sessions deleted more than 30 days ago").

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `sessionId` | `Indexed(str, unique=True)` | Frontend-generated unique session ID |
| `pocket` | `str | None` | Associated pocket ID |
| `group` | `str | None` | Associated group ID |
| `agent` | `str | None` | Associated agent ID |
| `workspace` | `Indexed(str)` | Parent workspace |
| `owner` | `str` | Session creator user ID |
| `title` | `str` | Session title (default: "New Chat") |
| `lastActivity` | `datetime` | Last activity timestamp |
| `messageCount` | `int` | Total messages in session |
| `deleted_at` | `datetime | None` | Soft deletion timestamp |

## Known Gaps

- The `messageCount` is denormalized and must be incremented externally whenever a message is added. If the increment is missed (e.g., due to a crash), the count drifts.
- No index on `deleted_at` — queries filtering out deleted sessions cannot use an index on that field alone.
- The second index `[("workspace", 1), ("group", 1), ("agent", 1)]` covers group+agent lookups but does not include a sort key, so ordering those results requires an additional sort stage.
