---
{
  "title": "Cloud Memory Backend Bootstrap — MongoDB Default Registration",
  "summary": "Bootstraps the MongoDB memory backend for cloud deployments by directly priming the `pocketpaw.memory.manager` singleton before Beanie is initialized, bypassing the file-based config that would otherwise retain a stale `memory_backend: \"file\"` setting. Respects the `POCKETPAW_MEMORY_BACKEND` environment variable as an explicit user override.",
  "concepts": [
    "memory bootstrap",
    "MongoMemoryStore",
    "POCKETPAW_MEMORY_BACKEND",
    "settings cache",
    "lru_cache",
    "init_cloud_db",
    "Beanie initialization",
    "environment variable override",
    "startup sequence",
    "backend registration"
  ],
  "categories": [
    "memory",
    "MongoDB",
    "startup",
    "configuration"
  ],
  "source_docs": [
    "95ea29d41e1e04ce"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`bootstrap.py` solves a specific initialization ordering problem in cloud deployments: PocketPaw's `Settings.load()` reads `~/.pocketpaw/config.json`, which may contain `memory_backend: "file"` from an earlier local installation. Without intervention, a cloud deployment that upgraded from a self-hosted install would silently use the file-based memory backend instead of MongoDB, losing all multi-tenant isolation and persistence guarantees.

The bootstrap module provides `register_default_backend()`, called from `init_cloud_db` before Beanie is initialized, to flip the default to `mongodb` in a way that takes precedence over the JSON config.

## How the Bypass Works

Rather than modifying `~/.pocketpaw/config.json` (which would be a side effect with system-wide consequences), the function takes two steps:

1. Sets `POCKETPAW_MEMORY_BACKEND=mongodb` in the process environment so any future `Settings.load()` call reads the correct value.
2. Calls `get_settings.cache_clear()` to invalidate the `@lru_cache` on the settings loader, ensuring the next call picks up the new env var rather than returning a cached `Settings` object with `memory_backend="file"`.

This approach is surgical — it only affects the running process, leaves no filesystem side effects, and is reversible by setting the env var to something else before the next `get_settings()` call.

## User Override Behavior

```python
def register_default_backend() -> None:
    explicit = os.environ.get("POCKETPAW_MEMORY_BACKEND")
    if explicit and explicit != "mongodb":
        logger.info("ee: POCKETPAW_MEMORY_BACKEND=%r set by user, not overriding", explicit)
        return
```

If `POCKETPAW_MEMORY_BACKEND` is already set to anything other than `"mongodb"`, the function is a no-op. This allows a cloud operator to explicitly select a different backend (e.g., for testing or a hybrid deployment) without fighting the bootstrap logic. The `!= "mongodb"` check means the function is idempotent — calling it when the env var is already `"mongodb"` still proceeds through the cache-clear step, which is harmless.

## Failure Modes

The `get_settings.cache_clear()` call is wrapped in a broad exception handler:

```python
try:
    from pocketpaw.config import get_settings
    get_settings.cache_clear()
except Exception:
    logger.debug("ee: failed to clear settings cache")
```

This failure path exists because the bootstrap is called early in startup — if the `pocketpaw` package import fails for any reason (version mismatch, partial install), the bootstrap should still proceed rather than crashing the entire startup sequence. The debug-level log ensures the failure is observable without being noisy in production.

## Placement in Startup Sequence

`register_default_backend()` must be called before `init_beanie()` / `init_cloud_db()` completes, because Beanie's document initialization reads the memory backend setting as part of setting up the ODM. Calling it after Beanie is already running would change the env var but leave existing connections using the wrong backend.

## Known Gaps

- The function does not verify that a MongoDB connection is actually reachable after setting the backend — a deployment with a bad `MONGO_URI` will silently fall through to the first query failing rather than a startup health check failure.
- The `get_settings.cache_clear()` call uses `type: ignore[import-untyped]` because `pocketpaw.config` has no stubs. If the cache-clearing API changes in the OSS package, this will fail silently at runtime.