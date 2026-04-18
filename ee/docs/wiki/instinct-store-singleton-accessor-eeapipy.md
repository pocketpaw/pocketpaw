---
{
  "title": "Instinct Store Singleton Accessor (ee/api.py)",
  "summary": "Provides a lazy-initialized singleton accessor for the InstinctStore, which backs the Instinct decision pipeline. This module exists so that core agent tools can import a stable entry point without coupling to the internal store implementation.",
  "concepts": [
    "singleton pattern",
    "InstinctStore",
    "lazy initialization",
    "SQLite",
    "instinct pipeline"
  ],
  "categories": [
    "enterprise",
    "data access",
    "design patterns"
  ],
  "source_docs": [
    "5f27d4cc458b4b0a"
  ],
  "backlinks": null,
  "word_count": 246,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Instinct Store Singleton Accessor

## Purpose

`ee/api.py` is the public API surface for the enterprise Instinct subsystem. It exposes `get_instinct_store()`, a singleton factory that lazily creates and returns the global `InstinctStore` instance. The core agent tools (`pocketpaw.tools.builtin.instinct_tools`) import from here rather than reaching into `ee.instinct.store` directly.

## Why a Singleton?

The `InstinctStore` wraps a SQLite database at `~/.pocketpaw/instinct.db`. Creating multiple store instances for the same database would risk:

1. **Connection contention** — multiple SQLite connections writing concurrently can cause locking errors
2. **State inconsistency** — separate instances would have separate in-memory caches
3. **Resource waste** — each instance opens its own connection pool

The singleton pattern (module-level `_store` variable with lazy init) ensures exactly one `InstinctStore` exists per process.

## Lazy Initialization

The store is created on first access, not at import time. This matters because:
- The `ee` package may be imported during module scanning even when enterprise features aren't enabled
- SQLite file creation should only happen when actually needed
- Import-time side effects make testing harder

## Integration Point

```
pocketpaw.tools.builtin.instinct_tools
    → from ee.api import get_instinct_store
        → ee.instinct.store.InstinctStore(~/.pocketpaw/instinct.db)
```

## Known Gaps

- No thread safety on the singleton creation — if two threads call `get_instinct_store()` simultaneously on first access, two stores could be created. In practice this is unlikely since FastAPI's startup typically triggers it once, but a threading lock would be more defensive.
- The database path is hardcoded to `~/.pocketpaw/instinct.db` with no override mechanism for testing or multi-tenant scenarios.