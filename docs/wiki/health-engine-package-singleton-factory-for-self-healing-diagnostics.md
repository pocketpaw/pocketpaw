---
{
  "title": "Health Engine Package: Singleton Factory for Self-Healing Diagnostics",
  "summary": "The health package `__init__.py` provides a lazy singleton factory `get_health_engine()` that creates and caches a `HealthEngine` instance on first access. The deferred import pattern means the `HealthEngine` class and all its check dependencies are only loaded when health checking is actually needed, keeping startup time minimal.",
  "concepts": [
    "HealthEngine",
    "singleton pattern",
    "lazy import",
    "TYPE_CHECKING",
    "diagnostic checks",
    "startup performance",
    "self-healing",
    "circular import prevention"
  ],
  "categories": [
    "health monitoring",
    "infrastructure",
    "startup",
    "diagnostics"
  ],
  "source_docs": [
    "6774108b83425f14"
  ],
  "backlinks": null,
  "word_count": 371,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `pocketpaw.health` package (`src/pocketpaw/health/__init__.py`) exports a single entry point: `get_health_engine()`. This function implements the module-level singleton pattern for the `HealthEngine`, which orchestrates diagnostic checks across configuration, API keys, connectivity, storage, and integrations.

## Lazy Singleton Pattern

```python
_instance: HealthEngine | None = None

def get_health_engine() -> HealthEngine:
    global _instance
    if _instance is None:
        from pocketpaw.health.engine import HealthEngine
        _instance = HealthEngine()
    return _instance
```

The import of `HealthEngine` is deferred inside the function body. At module import time, `pocketpaw.health` imports nothing from the engine or checks sub-packages. This matters because:

1. **Startup performance**: The health engine and its checks import several subsystems (config, secrets, connectivity). Deferring this until health checking is needed keeps the base import cost near zero.
2. **Circular import prevention**: If the engine or any check module imported `pocketpaw.health`, a circular import would occur at module load time. The deferred import breaks the cycle by only importing inside a function call, by which point all modules are already loaded.

The `TYPE_CHECKING` guard is used for the type annotation:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from pocketpaw.health.engine import HealthEngine
```

This satisfies type checkers (which see the annotation) without introducing a runtime import. The actual runtime import happens lazily in `get_health_engine()`.

## Why a Singleton?

The `HealthEngine` likely maintains state — check results, timestamps of last runs, background task handles. A singleton ensures that the dashboard, CLI health commands, and any other callers all see the same diagnostic state rather than spawning multiple background check loops.

## Usage Pattern

Callers throughout the codebase access the engine as:

```python
from pocketpaw.health import get_health_engine
engine = get_health_engine()
results = await engine.run_startup_checks()
```

This pattern is consistent with how PocketPaw manages other global services (message bus, automation store, audit logger), making the health engine feel native to the codebase.

## Known Gaps

- The singleton is not thread-safe. Two threads calling `get_health_engine()` simultaneously when `_instance is None` could create two `HealthEngine` instances. In an async context (single-threaded event loop) this is safe, but if the health endpoint is ever called from a thread pool, a lock would be needed.
- There is no `reset_health_engine()` or equivalent for testing. Tests that need to inject a mock engine must patch `pocketpaw.health._instance` directly.