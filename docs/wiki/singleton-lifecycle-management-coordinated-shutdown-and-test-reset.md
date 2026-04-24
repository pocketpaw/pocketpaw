---
{
  "title": "Singleton Lifecycle Management: Coordinated Shutdown and Test Reset",
  "summary": "The lifecycle module provides a central registry for singletons that need graceful shutdown on app teardown and deterministic reset between test runs. It decouples teardown orchestration from individual module implementations by letting each module register its own callbacks.",
  "concepts": [
    "lifecycle management",
    "singleton pattern",
    "graceful shutdown",
    "test reset",
    "async callbacks",
    "registry pattern",
    "teardown orchestration",
    "FastAPI lifespan"
  ],
  "categories": [
    "application lifecycle",
    "testing infrastructure",
    "async patterns",
    "resource management"
  ],
  "source_docs": [
    "fd311a1958818915"
  ],
  "backlinks": null,
  "word_count": 475,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw runs multiple long-lived singletons — schedulers, MCP managers, connection pools — that hold resources like open network sockets, background tasks, and file handles. Without coordinated teardown, these linger after the app exits, causing port conflicts, leaked connections, and flaky tests.

`lifecycle.py` solves this with a minimal registry pattern: modules call `register()` at initialization time, and the app teardown path calls `shutdown_all()` once. Tests call `reset_all()` between cases to restore singletons to their initial state without restarting the process.

## Registry Design

The registry is a module-level dict:

```python
_registry: dict[str, tuple[Callable | None, Callable | None]] = {}
```

Keys are human-readable names (`"scheduler"`, `"mcp_manager"`). Values are `(shutdown_callback, reset_callback)` tuples. Either callback can be `None` — a singleton that only needs test reset doesn't have to provide a shutdown callback, and vice versa.

## Why Not `atexit`?

Python's `atexit` module runs registered functions at interpreter exit, but it does not support async callbacks, cannot be selectively triggered during tests, and provides no ordering guarantees. `shutdown_all()` supports both async and sync callbacks via `asyncio.iscoroutine()` detection, making it suitable for FastAPI lifespan handlers.

## Graceful Shutdown

```python
async def shutdown_all() -> None:
    for name, (shutdown_cb, _) in list(_registry.items()):
        if shutdown_cb is None:
            continue
        try:
            result = shutdown_cb()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning("Error shutting down %s", name, exc_info=True)
```

Two design choices here prevent cascading failures:

1. **Errors are caught per-singleton**: if the scheduler shutdown throws, the MCP manager still gets its chance to close connections. Without this, one bad teardown would leave all subsequent singletons in an unclean state.
2. **`list(_registry.items())` copies the iterator**: if a shutdown callback modifies the registry (e.g., unregistering itself), it won't corrupt the iteration.

## Test Reset

`reset_all()` calls sync reset callbacks in registration order. This is intentionally sync-only — test teardown in pytest runs in a sync context, and mixing async here would require `asyncio.run()` nesting that breaks on some event loop configurations. Reset callbacks typically set a module-level variable back to `None`, allowing the next test to re-create the singleton fresh.

## Registration Pattern

```python
# In scheduler.py
from pocketpaw import lifecycle

_scheduler: Scheduler | None = None

def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler

lifecycle.register(
    "scheduler",
    shutdown=lambda: get_scheduler().shutdown(),
    reset=lambda: globals().update(_scheduler=None),
)
```

## Known Gaps

- **No ordering guarantees**: singletons shut down in dictionary insertion order (Python 3.7+), but there is no explicit dependency graph. If the MCP manager depends on the scheduler being alive during its own shutdown, the order matters and is currently implicit.
- **No registration deduplication warning**: calling `register()` twice with the same name silently overwrites the previous callbacks. A duplicate registration during module reload could drop a shutdown callback.
- **Reset callbacks are sync-only**: any singleton that requires async teardown for reset (e.g., draining an async queue) cannot use `reset_all()` directly.