---
{
  "title": "Smoke Test: MemoryManager Singleton Stays Live After Backend Swap",
  "summary": "This test reproduces a real bug where `AgentLoop` captured `get_memory_manager()` at construction time — before cloud database initialization — and then held a stale reference to the file-based store even after the MongoDB backend was registered. It verifies that the singleton's internal store is swapped in-place so pre-captured references automatically see the new backend.",
  "concepts": [
    "MemoryManager",
    "singleton",
    "FileMemoryStore",
    "MongoMemoryStore",
    "register_default_backend",
    "AgentLoop",
    "backend swap",
    "late binding",
    "init_cloud_db",
    "startup sequence",
    "smoke test"
  ],
  "categories": [
    "testing",
    "memory",
    "backend initialization",
    "agent runtime"
  ],
  "source_docs": [
    "d75b45f1c18ae165"
  ],
  "backlinks": null,
  "word_count": 435,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Bug This Test Was Written For

PocketPaw's startup sequence has a timing problem: `AgentLoop` constructs itself (and caches `self.memory = get_memory_manager()`) before `init_cloud_db` runs. When the enterprise cloud module bootstraps, it calls `register_default_backend()`, which flips the `MemoryManager` singleton's internal `_store` from `FileMemoryStore` to `MongoMemoryStore`. But if the `AgentLoop` held a direct reference to the old `MemoryManager` object, it would keep using the file store — silently writing to disk instead of MongoDB.

This is a classic singleton-with-late-binding bug. The fix requires that `register_default_backend()` mutate the **existing** singleton in place rather than creating a new one.

## Test Structure

The test runs in three phases that mirror the real startup sequence:

**Phase 1 — Before init:**
```python
manager = get_memory_manager()
assert isinstance(manager._store, FileMemoryStore)
agent_cached_memory = manager  # simulates AgentLoop.__init__
```

**Phase 2 — Cloud bootstrap:**
```python
await init_beanie(...)
register_default_backend()
```

**Phase 3 — Verify cached reference updated:**
```python
assert isinstance(agent_cached_memory._store, MongoMemoryStore)
```

If `register_default_backend` replaces the singleton rather than mutating it, `agent_cached_memory._store` would still be a `FileMemoryStore` and the test would fail with exit code `3`.

## Write-Through Verification

The test goes beyond checking the type — it actually writes through the cached reference and confirms the message lands in MongoDB:

```python
await agent_cached_memory.add_to_session(key, "user", "agent-loop path works")
rows = await Message.find({"session_key": key}).to_list()
assert len(rows) == 1 and rows[0].context_type == "pocket"
```

This is stronger than a type assertion because it exercises the real I/O path. The `context_type="pocket"` check confirms the mongo store is in use (the file store would never produce a MongoDB document).

## Cleanup and Global State Reset

After the test, the script explicitly resets the global manager:

```python
import pocketpaw.memory.manager as _mm
_mm._manager = None
```

This is important because smoke scripts are sometimes run in sequence within the same Python process. If `_manager` is left pointing at a MongoDB store with a dropped database, the next script that calls `get_memory_manager()` would start with a broken backend. Resetting to `None` forces lazy re-initialization on the next call.

## Why This Pattern Is Fragile

The singleton approach works but is sensitive to import order and call timing. Any code that calls `get_memory_manager()` during module import (before the app starts) will get the file backend. The real mitigation is that `AgentLoop` should call `get_memory_manager()` lazily (inside methods) rather than at construction, but this test ensures the in-place swap works as a safety net.

## Known Gaps

The test does not cover concurrent access — if multiple coroutines call `get_memory_manager()` simultaneously during the backend swap, there could be a brief window where some callers get the old store. A thread-safety assertion is absent.