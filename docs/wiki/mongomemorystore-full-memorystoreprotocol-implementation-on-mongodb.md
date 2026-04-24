---
{
  "title": "MongoMemoryStore — Full MemoryStoreProtocol Implementation on MongoDB",
  "summary": "The production MongoDB implementation of PocketPaw's `MemoryStoreProtocol`, bridging the OSS memory interface to the cloud's Beanie ODM schema. SESSION entries map to the `messages` collection; LONG_TERM and DAILY facts map to `memory_facts`. The store also manages session metadata lifecycle and normalizes session key formats between the message bus and the database.",
  "concepts": [
    "MongoMemoryStore",
    "MemoryStoreProtocol",
    "Beanie ODM",
    "session key normalization",
    "memory_facts",
    "messages collection",
    "multi-tenant isolation",
    "session auto-creation",
    "lastActivity",
    "messageCount",
    "LONG_TERM memory",
    "DAILY memory",
    "SESSION memory",
    "_touch_session"
  ],
  "categories": [
    "memory",
    "MongoDB",
    "architecture",
    "multi-tenancy"
  ],
  "source_docs": [
    "c812d9d135521fa4"
  ],
  "backlinks": null,
  "word_count": 562,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MongoMemoryStore` is the cloud memory adapter. It implements every method of `pocketpaw.memory.protocol.MemoryStoreProtocol` using MongoDB via the Beanie ODM, enabling the rest of the PocketPaw runtime to use agents' memories without knowing they are stored in MongoDB.

The store bridges two distinct storage concerns:
- **SESSION entries** → stored in the `messages` collection as pocket-context rows
- **LONG_TERM / DAILY entries** → stored in the `memory_facts` collection via `MemoryFactDoc`

## Session Key Normalization

The pocketpaw message bus forms session keys as `"{channel}:{chat_id}"` (colon-separated), while `Session.sessionId` and the UI use `"{channel}_{chat_id}"` (underscore). Without normalization, a chat session key from the bus could not be joined against session records in MongoDB.

```python
_KNOWN_BUS_CHANNELS = frozenset({"websocket", "telegram", "discord", "slack", "whatsapp", "cli"})

def _normalize_session_key(key: str) -> str:
    ...
```

The normalizer rewrites only known channel prefixes to prevent unintended rewrites when a session key happens to contain a colon for other reasons. Unknown prefixes are logged as warnings so channel drift between the bus and this list is visible.

## Session Auto-Creation

When `save()` writes a SESSION entry for a `session_key` that has no corresponding `Session` document, the store auto-creates one. This implements the "start chatting → session appears in the sidebar" UX without requiring a separate `POST /sessions` call first. The auto-created session has a minimal schema (just `sessionId`, `workspace`, and timestamps) and is updated with full metadata by the session router when the user explicitly names or configures the session.

## Session Metadata Upkeep

Every time `save()` writes a SESSION entry, it calls `_touch_session()` to increment `messageCount` and refresh `lastActivity`. This is done in the adapter rather than the session router because the adapter is the sole write path for chat turns — all messages flow through `save()` regardless of channel. Centralizing this touch here means session metadata stays accurate for both API-initiated and bot-initiated messages.

## Multi-Tenant Isolation

For SESSION entries, the store resolves `workspace_id` from the linked `Session.workspace` at write time. For LONG_TERM/DAILY entries, callers populate `entry.metadata["workspace_id"]`. Protocol-level methods (`get_by_type`, `search`) do not apply workspace filtering — they implement the OSS `MemoryStoreProtocol` contract, which is unscoped. Workspace-scoped reads go through adapter-specific helpers:

- `list_facts_in_workspace(workspace_id, memory_type, user_id, limit)` — queries `memory_facts` by workspace
- `get_session_in_workspace(session_key, workspace_id)` — queries `messages` by session + workspace

This two-tier design preserves the OSS protocol contract while adding the tenant-scoped helpers needed by the cloud layer without forking the protocol.

## Translation Functions

`_message_to_entry` and `_fact_to_entry` translate Beanie document instances into `MemoryEntry` objects for the protocol layer. This keeps the ODM types internal to the store — the rest of the runtime only sees the protocol type.

## Error Handling

`delete()` catches `InvalidId` errors from bson when the caller passes a malformed ObjectId string (e.g., a non-hex string), returning `False` rather than raising. This defensive handling prevents agent code from crashing when it passes a memory entry ID it received from a different backend.

## Known Gaps

- `search()` implements keyword matching in Python rather than delegating to MongoDB text search or Atlas Search. For large `memory_facts` collections this is a full table scan.
- `_load_session_index_async()` caches session lookups in an in-memory dict that is never invalidated. In long-running processes with many sessions, this dict grows unbounded.
- The `clear_session()` method deletes all messages for a session_key but does not delete the corresponding `Session` document — the session remains in the sidebar with a zero message count.