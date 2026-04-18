---
{
  "title": "MongoDB Connection and Beanie ODM Initialization",
  "summary": "Manages the MongoDB connection lifecycle for the cloud module. Initializes the Beanie ODM with all document models on startup and provides a clean shutdown path. Uses a module-level singleton client pattern.",
  "concepts": [
    "MongoDB",
    "Beanie ODM",
    "connection lifecycle",
    "AsyncMongoClient",
    "singleton pattern",
    "database initialization"
  ],
  "categories": [
    "cloud",
    "shared",
    "database",
    "infrastructure"
  ],
  "source_docs": [
    "bdd892f5a8c21695"
  ],
  "backlinks": null,
  "word_count": 248,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# MongoDB Connection and Beanie ODM Initialization

## Purpose

This module is the single entry point for MongoDB connectivity in the cloud layer. It initializes the async MongoDB client and sets up Beanie ODM with all document models, making them queryable throughout the application.

## Connection Lifecycle

### Initialization

`init_cloud_db()` performs three steps:
1. Creates an `AsyncMongoClient` from the provided URI (defaults to `mongodb://localhost:27017/paw-cloud`)
2. Extracts the database name from the URI by splitting on `/` and `?` — handles both `mongodb://host/dbname` and `mongodb://host/dbname?options` formats
3. Calls `init_beanie()` with all document models imported from `ee.cloud.models.ALL_DOCUMENTS`

The database name extraction (`rsplit("/", 1)[-1].split("?")[0]`) is a defensive parse that handles query parameters in the URI. Falls back to `"paw-cloud"` if extraction yields an empty string.

### Shutdown

`close_cloud_db()` closes the client and sets the global to `None`. This ensures clean shutdown and prevents stale connections.

### Client Access

`get_client()` exposes the current client for modules that need direct MongoDB access beyond what Beanie provides (e.g., raw aggregation pipelines).

## Design Notes

The module-level `_client` singleton pattern means only one MongoDB connection pool exists per process. This is intentional — Beanie is initialized once at startup and all document classes share the same connection.

## Known Gaps

- No connection retry logic — if MongoDB is unavailable at startup, `init_cloud_db()` will raise and the app won't start
- No health check or reconnection mechanism for dropped connections during runtime
- `get_client()` returns `None` if called before initialization — callers must handle this
