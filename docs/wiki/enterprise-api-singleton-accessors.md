---
{
  "title": "Enterprise API Singleton Accessors",
  "summary": "`ee/api.py` provides module-level singleton factory functions for the two main enterprise data stores — `InstinctStore` and `PawPrintStore` — so that both agent tool code and domain routers can reach a shared, lazily-initialised database instance without dependency injection.",
  "concepts": [
    "singleton pattern",
    "InstinctStore",
    "PawPrintStore",
    "lazy initialisation",
    "SQLite",
    "dependency injection",
    "agent tools",
    "module-level globals",
    "database path"
  ],
  "categories": [
    "data access",
    "enterprise",
    "architecture"
  ],
  "source_docs": [
    "eb7255f58e4a8957"
  ],
  "backlinks": null,
  "word_count": 404,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

PocketPaw's enterprise layer has two persistent SQLite stores: `InstinctStore` (the decision/approval pipeline database) and `PawPrintStore` (the widget event database). Both need to be shared singletons — if two callers each instantiated their own store, they would open competing connections to the same file and risk write conflicts or stale reads.

However, FastAPI's dependency injection system is only available inside request handlers. The open-source agent tools (`pocketpaw.tools.builtin.instinct_tools`) and the `ee/paw_print/router.py` are wired up at import time, before any request context exists. `ee/api.py` solves this by providing plain function accessors that any module can call without needing a `Depends()` chain.

## Implementation

```python
from ee.api import get_instinct_store

store = get_instinct_store()   # safe to call anywhere
```

Both functions use a module-level `None` guard (the classic Python lazy singleton):

```python
_store: InstinctStore | None = None

def get_instinct_store() -> InstinctStore:
    global _store
    if _store is None:
        _store = InstinctStore(_DB_PATH)
    return _store
```

The first call creates the store and opens the SQLite file; subsequent calls return the cached instance. This is safe in a single-process async server (FastAPI runs in one event loop) because Python's GIL prevents the `if _store is None` check from racing.

## Database Paths

Both databases live under `~/.pocketpaw/` — the canonical PocketPaw data directory:

- `InstinctStore`: `~/.pocketpaw/instinct.db`
- `PawPrintStore`: `~/.pocketpaw/paw_print.db`

Using `Path.home()` rather than a hardcoded path ensures the module works on any OS and under any user account, which matters for the desktop app (Tauri) use case where the home directory varies.

## Why Not Dependency Injection?

FastAPI's `Depends()` mechanism would be cleaner for testability, but it requires all callers to be FastAPI route handlers or sub-dependencies. The instinct tools run inside the agent runtime, which is invoked by the agent pool — not by a FastAPI route — so they cannot use `Depends()`. The singleton pattern is a deliberate trade-off: slightly harder to unit-test in isolation, but compatible with the full call graph.

## Known Gaps

- There is no thread-safety guard (e.g. `threading.Lock`) around the `if _store is None` block. In the current single-process async model this is fine, but a multi-worker deployment (e.g. `gunicorn -w 4`) would create one store per worker process, each with its own SQLite file handle. SQLite's WAL mode handles concurrent readers, but concurrent writers from multiple processes can cause lock contention.
- No mechanism exists to close or reset the stores, which makes isolated unit testing require monkeypatching the module-level globals.