---
{
  "title": "Long-Term Memory API — CRUD, Settings, and Stats",
  "summary": "The memory router exposes endpoints for managing PocketPaw's long-term memory store, configuring which backend is active (file vs. mem0), and retrieving usage statistics. It bridges the REST surface to the pluggable MemoryManager, ensuring settings changes take effect immediately by flushing cached singletons.",
  "concepts": [
    "long-term memory",
    "MemoryManager",
    "mem0",
    "memory backend",
    "settings whitelist",
    "cache invalidation",
    "scope guard",
    "require_scope",
    "ISO-8601 timestamps",
    "capability detection"
  ],
  "categories": [
    "memory",
    "API",
    "configuration"
  ],
  "source_docs": [
    "06403bb179d413ee"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports persistent memory so companions can remember facts about users across sessions. The `memory.py` router is the REST interface for that capability. It is deliberately scoped behind the `memory` OAuth scope so only callers with explicit memory permission can read or mutate stored facts.

## Endpoints

### `GET /memory/long_term`

Fetches stored long-term memories from the active backend. The `limit` query parameter is clamped between 1 and 500, preventing accidental full-table scans on large mem0 deployments. The route normalises timestamps to ISO-8601 strings at the API layer because different backends (SQLite, Qdrant, Chroma) can return `datetime` objects, raw strings, or timestamps, and the client must not need to know the difference.

### `DELETE /memory/long_term/{entry_id}`

Deletes a single memory entry by ID. The 404 guard is important: if a client deletes the same entry twice (e.g., double-click, retry after network error), the second request gets an explicit 404 rather than a silent success. This makes client-side confirmation UI reliable.

### `GET /memory/settings`

Returns the current memory backend configuration as a flat JSON object. Reading from `Settings.load()` (rather than a cached singleton) ensures the response reflects any on-disk changes made outside the current process, such as manual config edits.

### `POST /memory/settings`

Accepts a JSON body and applies only the keys listed in `_MEMORY_CONFIG_KEYS` — a whitelist that maps API field names to settings attributes. This pattern prevents arbitrary attribute injection: a payload with `{"admin_token": "hack"}` would simply be ignored because `admin_token` is not in the whitelist.

After persisting the new values, the handler calls `get_settings.cache_clear()` and `get_memory_manager(force_reload=True)`. Without these two calls, the running process would keep serving requests against the old backend until restart. The cache-clear/force-reload pattern is the idempotency guard that makes live configuration changes safe.

### `GET /memory/stats`

Returns backend-specific statistics. The check `if hasattr(store, "get_memory_stats")` handles the capability mismatch between the simple file backend (which has no stats) and mem0-backed stores (which can return counts, embedding dimensions, etc.). The fallback response explicitly hints to users that stats require the mem0 backend, steering them toward the right configuration.

## Scope Guard

All routes share a single router-level `Depends(require_scope("memory"))`. This means adding a new memory route automatically inherits the access control — a developer cannot accidentally expose a memory endpoint without authentication.

## Design Notes

The `_MEMORY_CONFIG_KEYS` dict is intentionally a 1:1 mapping (both keys are the same string). This looks redundant but is an explicit list that can be audited. If the internal `Settings` attribute name ever needs to diverge from the API name, the mapping is already in place.

## Known Gaps

- The `delete` endpoint calls `manager._store.delete(entry_id)` directly rather than going through the high-level `MemoryManager` API. This bypasses any eviction or hook logic the manager might need to run, and the leading underscore signals this is an internal detail. A future refactor should surface `delete_by_id` as a first-class method on `MemoryManager`.
- There is no bulk-delete endpoint. Callers who want to clear all memories must call delete one entry at a time or restart with a fresh config.
