---
{
  "title": "Cloud Memory Package — MongoMemoryStore Public API",
  "summary": "The `ee.cloud.memory` package's public interface, which re-exports `MongoMemoryStore` as the single entry point for MongoDB-backed agent memory in cloud deployments. The module is intentionally minimal, acting as a stable import boundary between the cloud layer and the rest of the application.",
  "concepts": [
    "MongoMemoryStore",
    "memory backend",
    "package re-export",
    "cloud memory",
    "OSS compatibility",
    "Beanie",
    "memory manager",
    "ee.cloud.memory"
  ],
  "categories": [
    "memory",
    "MongoDB",
    "architecture"
  ],
  "source_docs": [
    "c30905e67341435d"
  ],
  "backlinks": null,
  "word_count": 240,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee/cloud/memory/__init__.py` file defines the public API of the `ee.cloud.memory` package. Its only role is to re-export `MongoMemoryStore` from `ee.cloud.memory.mongo_store`, making `from ee.cloud.memory import MongoMemoryStore` the canonical import path.

## Why a Dedicated Package

The memory subsystem is separated from the broader `ee.cloud.models` package for two reasons:

1. **Deployment optionality** — the OSS PocketPaw runtime supports multiple memory backends (file, in-memory, SQLite). The cloud layer replaces the default backend with MongoDB. Isolating MongoDB-specific code in `ee.cloud.memory` means the OSS runtime never needs to import MongoDB dependencies.

2. **Testability** — unit tests for the memory layer can import just `ee.cloud.memory` and mock Beanie without triggering the full model registry initialization that `ee.cloud.models` requires.

## Re-export Pattern

```python
from ee.cloud.memory.mongo_store import MongoMemoryStore
__all__ = ["MongoMemoryStore"]
```

The explicit `__all__` ensures that `from ee.cloud.memory import *` yields only `MongoMemoryStore`, preventing accidental leakage of internal helpers from `mongo_store.py`.

## Relationship to Other Modules

- `ee.cloud.memory.bootstrap` reads this package indirectly — it imports `MongoMemoryStore` to prime the memory manager singleton before Beanie is initialized.
- `ee.cloud.memory.documents` provides the `MemoryFactDoc` Beanie document used by `MongoMemoryStore` internally.
- The runtime's `pocketpaw.memory.manager` singleton is what actually gets wired up to `MongoMemoryStore` via the bootstrap process.

## Known Gaps

- No async initialization hook is exported here. If MongoDB connection pooling needs to be warmed up before the first request, callers currently rely on Beanie's own init sequence rather than any explicit `startup()` function in this package.