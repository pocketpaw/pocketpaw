---
{
  "title": "FabricStore — Async SQLite Backend for the Ontology Layer",
  "summary": "`FabricStore` is the legacy async SQLite persistence layer for Fabric's ontology data, managing object types, object instances, and directional links. It uses lazy schema initialization, parameterized queries throughout, and a manual cascade-delete pattern because SQLite foreign key enforcement is not enabled by default.",
  "concepts": [
    "FabricStore",
    "aiosqlite",
    "lazy schema initialization",
    "cascade delete",
    "parameterized query",
    "merge update semantics",
    "SQL injection prevention",
    "source deduplication",
    "list_links",
    "CREATE TABLE IF NOT EXISTS"
  ],
  "categories": [
    "fabric",
    "SQLite",
    "persistence",
    "async",
    "ontology"
  ],
  "source_docs": [
    "2835825a6cf10b6b"
  ],
  "backlinks": null,
  "word_count": 468,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Schema Design

The three core tables mirror the Fabric data model:

- `fabric_object_types` — type definitions with `properties_schema` stored as a JSON blob.
- `fabric_objects` — instances with `properties` as a JSON blob. The `source_connector + source_id` pair enables connector deduplication.
- `fabric_links` — directional edges between objects with `link_type` and optional `properties`.

Indexes are created on the most common filter columns: `type_id` for object filtering, `source_connector + source_id` for connector sync deduplication, and `from_object_id + to_object_id + link_type` for link traversal.

## Lazy Schema Initialization

`_ensure_schema()` runs `CREATE TABLE IF NOT EXISTS` once per `FabricStore` instance, guarded by `self._initialized`. This lazy approach means the schema is created automatically on first use — no migration tooling required for a fresh install. The downside is that schema evolution (adding columns) requires manual migration scripts; the SQLite store has no built-in migration path.

## Manual Cascade Deletes

SQLite supports `ON DELETE CASCADE` via foreign keys, but it is disabled by default (`PRAGMA foreign_keys = ON` must be called per connection). Rather than relying on this pragma being set, `remove_type` and `remove_object` issue explicit `DELETE` statements for dependent rows before deleting the parent. This is more verbose but reliable across any SQLite configuration.

```python
# remove_type: delete links → objects → type
await db.execute(
    "DELETE FROM fabric_links WHERE from_object_id IN "
    "(SELECT id FROM fabric_objects WHERE type_id = ?) ...",
    (type_id, type_id),
)
```

## Parameterized Queries and SQL Injection

`list_links()` is the most complex query method: it builds a dynamic `WHERE` clause from optional `from_id`, `to_id`, and `link_type` parameters. The clause is assembled by appending to a `conditions: list[str]` of `?` placeholders and a matching `params: list[Any]`. The constructed SQL string never contains user-supplied values — only fixed column names. This is the correct pattern for dynamic-but-safe SQL construction.

## Update Semantics

`update_object()` implements merge semantics: `merged = {**existing.properties, **new_properties}`. New keys are added, existing keys are overwritten, absent keys are preserved. This matches the journal path's `object_updated_payload` partial dict convention — the two stores behave consistently on updates even though their backends differ.

## Type Lookups

`get_type_by_name()` uses `LOWER(name) = LOWER(?)` for case-insensitive matching. This prevents duplicate types that differ only in case ("Customer" vs "customer") while still preserving the display name as entered.

## Cluster C / PR3 Addition

`list_links()` was added in Cluster C / PR3 to back the `GET /fabric/links` endpoint. Before this addition the Links sub-tab in `PocketDataPanel` rendered hardcoded placeholder data.

## Known Gaps

- The `_initialized` flag is per-instance, not process-global. If `FabricStore` is instantiated per-request (as the router currently does), `_ensure_schema()` re-executes the `CREATE TABLE IF NOT EXISTS` script on every request. The script is idempotent but the extra round-trip is wasteful.
- No `query` method supports `linked_to` traversal in the objects result — graph queries require a separate `get_linked_objects()` call.