---
{
  "title": "FabricStore: Async SQLite Persistence for the Ontology Layer",
  "summary": "Async SQLite store implementing CRUD operations for object types, objects, and links in the Fabric ontology. Uses aiosqlite for non-blocking database access, lazy schema initialization, and JSON serialization for nested properties. Supports graph-style queries via link traversal.",
  "concepts": [
    "FabricStore",
    "aiosqlite",
    "async SQLite",
    "schema initialization",
    "cascade delete",
    "JSON columns",
    "bidirectional link traversal",
    "dynamic SQL",
    "ontology persistence"
  ],
  "categories": [
    "fabric",
    "ontology",
    "storage",
    "SQLite",
    "async"
  ],
  "source_docs": [
    "c9d46a44aafa9b64"
  ],
  "backlinks": null,
  "word_count": 610,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# FabricStore: Async SQLite Persistence for the Ontology Layer

## Purpose

`FabricStore` is the persistence layer for Fabric's ontology data. It stores object types (schemas), objects (instances), and links (relationships) in SQLite, using JSON columns for flexible property storage. The async design via `aiosqlite` prevents database I/O from blocking the FastAPI event loop.

## Schema

Three tables with indexes:

### `fabric_object_types`
Stores type definitions. `properties_schema` is a JSON array of PropertyDef objects — this keeps the schema flexible without requiring ALTER TABLE when property definitions change.

### `fabric_objects`
Object instances. `type_name` is denormalized from the type table for display convenience. `properties` is JSON. `source_connector` + `source_id` enable data lineage tracking.

### `fabric_links`
Directional relationships. Indexed on both `from_object_id` and `to_object_id` for bidirectional link traversal.

### Indexes
- `idx_objects_type` — fast type-filtered queries
- `idx_objects_source` — fast connector-based lookups (deduplication)
- `idx_links_from/to` — fast graph traversal in both directions
- `idx_links_type` — fast link-type filtering

## Lazy Schema Initialization

`_ensure_schema()` runs `CREATE TABLE IF NOT EXISTS` on first access and sets `_initialized = True` to skip on subsequent calls. This avoids requiring an explicit init step and means the database file is created on first use. The `IF NOT EXISTS` clause makes it idempotent — safe to call repeatedly without corrupting existing data.

## Key Operations

### Type Management
- `define_type` — Serializes PropertyDef list to JSON for storage
- `get_type` / `get_type_by_name` — Name lookup is case-insensitive (`LOWER()`)
- `remove_type` — Cascade deletes: removes links involving type's objects, then objects, then the type itself. Without this cascade, orphaned objects and links would accumulate.

### Object Management
- `create_object` — Resolves type_name from type_id for denormalization
- `update_object` — Merges new properties with existing ones (not a full replace), preserving properties the caller didn't mention
- `remove_object` — Cascade deletes associated links first

### Link Management
- `link` / `unlink` — Simple create/delete
- `get_linked_objects` — Bidirectional traversal (follows links in both directions), optionally filtered by link type

### Query Engine
The `query` method builds dynamic SQL based on `FabricQuery` parameters:
1. Type filter (by ID or name, case-insensitive)
2. Link filter (subquery finding objects linked to a given object, optionally by link type)
3. Pagination (LIMIT/OFFSET)
4. Separate count query for total results

The link filter uses a UNION of two subqueries (from->to and to->from) to handle bidirectional link traversal.

### Stats
Simple count queries across all three tables — used by the dashboard for overview metrics.

## Design Decisions

- **Connection-per-operation**: Each method opens and closes its own `aiosqlite` connection via `async with self._conn()`. This is simpler than connection pooling and appropriate for SQLite (which handles concurrent readers well).
- **JSON columns**: Properties are stored as JSON strings rather than normalized columns. This trades query performance for schema flexibility — agents can define arbitrary properties without migrations.
- **No foreign key enforcement at runtime**: The schema declares REFERENCES but SQLite doesn't enforce foreign keys by default (requires `PRAGMA foreign_keys = ON`). The cascade deletes in `remove_type` and `remove_object` handle referential integrity manually.

## Known Gaps

- **No PRAGMA foreign_keys = ON**: Foreign key constraints in the schema are decorative. Manual cascade deletes compensate but don't protect against direct SQL manipulation.
- **No property filter support in queries**: `FabricQuery.filters` dict is accepted but never used in the `query()` method — only type and link filters are implemented.
- **No connection pooling**: Each operation opens a new connection. Fine for low-traffic local use but would need pooling for server deployments.
- **Timestamps use SQLite's `datetime('now')` in defaults**: This is UTC in SQLite, but the Pydantic models use `datetime.now()` (local time) for default values — potential mismatch between DB-generated and Python-generated timestamps.
