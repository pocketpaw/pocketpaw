---
{
  "title": "Cloud DB Module - Backward-Compatible Database Shim",
  "summary": "`ee/cloud/db.py` is a one-line backward-compatibility shim that re-exports the database lifecycle functions from the canonical `ee.cloud.shared.db` module. It exists solely to preserve older import paths after the database logic was consolidated into the shared package.",
  "concepts": [
    "database shim",
    "backward compatibility",
    "init_cloud_db",
    "close_cloud_db",
    "get_client",
    "AsyncIOMotorClient",
    "Beanie",
    "re-export",
    "shared module"
  ],
  "categories": [
    "cloud EE",
    "database",
    "MongoDB",
    "architecture"
  ],
  "source_docs": [
    "0227cc85759e9ceb"
  ],
  "backlinks": null,
  "word_count": 251,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

```python
# Backward compat -- delegates to shared/db.py
from ee.cloud.shared.db import close_cloud_db, get_client, init_cloud_db  # noqa: F401
```

This three-symbol re-export is the entire content of `ee/cloud/db.py`. When the database initialisation logic was consolidated into `ee.cloud.shared.db`, all existing `from ee.cloud.db import ...` call sites continued to work without modification.

## Why Centralise in `shared/db.py`?

The `ee.cloud.shared` package holds infrastructure concerns shared across the cloud EE modules: database connections, error classes, timing utilities, and event definitions. Moving database lifecycle management there prevents duplication and ensures that connection pool settings are configured in exactly one place.

## The Three Exported Symbols

- **`init_cloud_db`** - called at application startup to establish the MongoDB connection and register Beanie document models. Must complete before any request handler uses the database.
- **`close_cloud_db`** - called at application shutdown to cleanly close the connection. Prevents connection pool exhaustion in test environments where the app starts and stops repeatedly.
- **`get_client`** - returns the live `AsyncIOMotorClient` instance for code that needs the raw driver level (transactions, aggregation pipelines not supported by Beanie).

## Migration Pattern

The `# noqa: F401` annotation is the tell: these imports are re-exported for external callers, not used locally. Linters treat re-exports as unused imports by default; `noqa` suppresses the warning while communicating intent to future maintainers.

## Known Gaps

- This shim carries no deprecation notice. Callers importing from `ee.cloud.db` instead of `ee.cloud.shared.db` will continue to work but are silently on the older path. Adding a `warnings.warn(DeprecationWarning)` would nudge callers to update.