---
{
  "title": "MongoDB Connection and Beanie ODM Initialization",
  "summary": "This module manages the lifecycle of the MongoDB connection and initializes the Beanie ODM with all document models required by the cloud EE layer. It also bootstraps the memory backend after Beanie is ready to prevent race conditions on first database access.",
  "concepts": [
    "Beanie ODM",
    "MongoDB",
    "AsyncMongoClient",
    "init_beanie",
    "document models",
    "memory backend",
    "circular import",
    "database lifecycle",
    "MemoryFactDoc",
    "MongoMemoryStore"
  ],
  "categories": [
    "database",
    "initialization",
    "cloud EE"
  ],
  "source_docs": [
    "631bcb8b7cb1f190"
  ],
  "backlinks": null,
  "word_count": 385,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/shared/db.py` is the single entry point for establishing a MongoDB connection in the PocketPaw cloud EE layer. It wraps Beanie's `init_beanie` and ensures that every document model — including those from separate packages like the memory subsystem — is registered in one place.

## Why Centralized Initialization Matters

Beanie requires all document models to be declared at startup before any queries run. If a model is registered late or forgotten, the first query against that collection either fails with a configuration error or silently creates an unindexed, unconfigured collection. By collecting every model in a single `init_cloud_db` call, the module guarantees that no code path hits an uninitialized collection.

## Database Name Extraction

The database name is extracted from the URI directly rather than being hardcoded:

```python
db_name = mongo_uri.rsplit("/", 1)[-1].split("?")[0] or "paw-enterprise"
```

This handles URIs with query parameters (e.g., `?authSource=admin`) without corrupting the database name, and falls back to `"paw-enterprise"` if the URI has no path component.

## Memory Backend Bootstrap Ordering

The `register_default_backend()` call happens after `init_beanie` completes:

```python
await init_beanie(database=db, document_models=documents)
register_default_backend()
```

This ordering is deliberate. The `MongoMemoryStore` uses Beanie models internally. If `register_default_backend()` were called before `init_beanie`, the store's first `.insert()` or `.find()` call could race against an uninitialized collection. By bootstrapping after Beanie, the memory backend is guaranteed to see a fully-configured database.

## MemoryFactDoc Separation

`MemoryFactDoc` is imported from `ee.cloud.memory.documents` rather than from the central `ee.cloud.models.ALL_DOCUMENTS` list. This separation exists to avoid a circular import: the memory package depends on cloud models, but `ALL_DOCUMENTS` lives in the models package. Merging the lists at the call site sidesteps the cycle without requiring a refactor of either package.

## Connection Lifecycle

The module holds a module-level `_client` reference. `close_cloud_db()` closes the client and clears the reference, making the module safe to reinitialize (e.g., in tests or on process restart). `get_client()` exposes the raw client for callers that need direct access outside Beanie's model layer.

## Known Gaps

- There is no retry or backoff on `AsyncMongoClient` construction or `init_beanie`. A transient MongoDB unavailability at startup would cause a hard crash rather than a graceful retry.
- The default URI `mongodb://localhost:27017/paw-enterprise` is appropriate for local development but relies on the caller always passing the correct production URI. There is no validation that the URI is well-formed before connecting.