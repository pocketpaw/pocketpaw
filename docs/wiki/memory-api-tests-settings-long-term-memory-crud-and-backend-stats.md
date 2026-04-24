---
{
  "title": "Memory API Tests: Settings, Long-Term Memory CRUD, and Backend Stats",
  "summary": "This test file covers PocketPaw's `/api/v1/memory` router, which exposes configuration for the memory backend (file store vs. mem0), CRUD operations over long-term memory entries, and per-backend statistics.",
  "concepts": [
    "memory backend",
    "mem0",
    "file store",
    "MemoryManager",
    "long-term memory",
    "memory settings",
    "get_by_type",
    "memory stats",
    "duck typing",
    "backend switching",
    "JSONL store"
  ],
  "categories": [
    "memory",
    "API",
    "testing",
    "agent configuration",
    "test"
  ],
  "source_docs": [
    "2f403b64e758729d"
  ],
  "backlinks": null,
  "word_count": 394,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports pluggable memory backends: a local JSONL file store and the mem0 vector-augmented memory system. The memory API lets the dashboard read and update backend settings, browse and delete stored long-term memories, and query backend-specific statistics. All operations delegate to `get_memory_manager()`, which returns the active `MemoryManager` singleton.

## Memory Settings (`GET /memory/settings`, `POST /memory/settings`)

`TestMemorySettings` verifies that:

- **GET** returns the full set of memory configuration fields: `memory_backend`, `memory_use_inference`, `mem0_llm_provider`, `mem0_llm_model`, `mem0_embedder_provider`, `mem0_embedder_model`, `mem0_vector_store`, `mem0_ollama_base_url`, and `mem0_auto_learn`. The test patches `Settings.load` to return a controlled mock so the fields can be asserted individually.
- **POST** saves updated settings. The test asserts `mock_s.save.assert_called_once()`, confirming the settings object is persisted after field updates. Without this assertion, a route that deserialises the request but never writes it to disk would pass a superficial status-code check.

## Long-Term Memory (`GET /memory/long_term`, `DELETE /memory/long_term/{id}`)

`TestMemoryLongTerm` covers four scenarios:

- **List**: Returns an array of memory objects, each with `id`, `content`, `created_at`, and `tags`. The manager's `get_by_type` is called with no explicit type argument, implying the route defaults to long-term memory type.
- **List with limit**: The `?limit=10` query parameter is forwarded to `get_by_type`. The test uses `assert_called_once()` to confirm the parameter is not silently dropped.
- **Delete success**: `mgr._store.delete(id)` returns `True`; response is 200. Accessing the private `_store` attribute directly is a test convenience — in production the manager method would be used.
- **Delete not found**: `_store.delete` returns `False`; the route returns 404. This distinguishes "deleted" from "was never there", which matters for client-side cache invalidation logic.

## Memory Stats (`GET /memory/stats`)

`TestMemoryStats` exercises a duck-typing check in the stats endpoint: if the memory store has no `get_memory_stats` attribute (i.e., it is the simple file store rather than a mem0 store), the endpoint returns `{"backend": "file"}` as a fallback. The test uses `spec=[]` on the mock to simulate an object with no methods, triggering this fallback path:

```python
store = MagicMock(spec=[])  # No get_memory_stats attr
```

This approach avoids hardcoding `isinstance` checks in the route — the duck-typing pattern keeps the route compatible with future backend types.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: switching backends at runtime (from file to mem0), what happens when `get_memory_manager()` raises (engine not initialised), or the stats response when mem0 is the active backend (the `get_memory_stats` happy path is not tested).