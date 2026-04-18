---
{
  "title": "Cloud Database Backward Compatibility Re-export",
  "summary": "A thin compatibility shim that re-exports database functions from `ee.cloud.shared.db`. Exists to preserve import paths after the database module was moved to the shared package.",
  "concepts": [
    "backward compatibility",
    "re-export",
    "database initialization",
    "shim module"
  ],
  "categories": [
    "database",
    "infrastructure",
    "compatibility"
  ],
  "source_docs": [
    "0a23c1d57a121264"
  ],
  "backlinks": null,
  "word_count": 92,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cloud Database Backward Compatibility Re-export

`cloud/db.py`

## Purpose

This module is a backward compatibility shim. The database initialization functions (`init_cloud_db`, `close_cloud_db`, `get_client`) were originally defined here but later moved to `ee.cloud.shared.db`. This file re-exports them so that existing code importing from `ee.cloud.db` continues to work without changes.

The `# noqa: F401` suppresses the "imported but unused" lint warning since the imports exist purely for re-export.

## Exported Functions

- `init_cloud_db` — Initialize the MongoDB/Beanie connection
- `close_cloud_db` — Clean up the database connection
- `get_client` — Get the underlying MongoDB client
