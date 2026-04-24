---
{
  "title": "Instinct Store — Async SQLite Persistence for the Decision Pipeline",
  "summary": "Provides all async SQLite read and write operations for the Instinct decision pipeline: action lifecycle management, audit logging, correction recording, reasoning traces, and Fabric object snapshots. It is the single source of truth for the decision state within a PocketPaw enterprise installation.",
  "concepts": [
    "InstinctStore",
    "aiosqlite",
    "action lifecycle",
    "audit log",
    "correction recording",
    "FabricObjectSnapshot",
    "SCHEMA_SQL",
    "count_corrections_by_path",
    "WAL mode",
    "row deserializer",
    "reasoning trace persistence"
  ],
  "categories": [
    "instinct engine",
    "data persistence",
    "SQLite",
    "enterprise edition"
  ],
  "source_docs": [
    "656efb34e1e0c92f"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`InstinctStore` is an async SQLite wrapper built on `aiosqlite`. It owns four distinct data concerns in a single database file: the action lifecycle, the audit log, the correction history, and fabric object snapshots. This co-location is intentional — all four tables participate in the same transactional domain (a decision event), and SQLite's WAL mode makes concurrent reads safe without a separate connection pool.

## Schema (SCHEMA_SQL)

The schema is defined as a module-level `SCHEMA_SQL` constant and applied lazily via `_ensure_schema()` on first connection. Using `CREATE TABLE IF NOT EXISTS` makes the schema idempotent — deployments that add new columns must still manage migrations separately, but the base creation is safe to run repeatedly.

## Action Lifecycle Methods

- **propose()** — inserts a new `Action` row with `status=PENDING`. Accepts an optional `reasoning_trace` (serialized as JSON into the `context` column) and `fabric_snapshots` (stored in a separate table). Returns the full hydrated `Action`.
- **pending() / pending_count()** — read paths for the dashboard's approval queue. `pending_count()` returns an integer scalar without deserializing rows, keeping it cheap for badge counts.
- **list_actions() / _query_actions()** — flexible query with optional status filter and limit. The private `_query_actions()` consolidates the WHERE clause construction.
- **for_pocket()** — returns all actions for a pocket regardless of status, used by agents reviewing history.

## Audit Log

`_log()` is the internal writer called by `log()`. The split exists so callers use the keyword-only public surface (`log(actor=..., event=..., description=..., **kwargs)`) while the full parameter list stays internal. `export_audit()` serializes the full audit log for a pocket to a JSON string, used by the router's export endpoint.

## Correction Methods

- **record_correction()** — inserts a `Correction` with its `patches` list serialized as JSON.
- **get_corrections_for_pocket() / get_corrections_for_action()** — read paths for the corrections endpoint and the soul bridge.
- **count_corrections_by_path()** — counts how many times a given `path` has been corrected in a pocket. This is the counter the `CorrectionSoulBridge` uses to decide when to promote an edit to procedural soul memory.

## Fabric Snapshot Methods

- **record_fabric_snapshot()** — writes an immutable `FabricObjectSnapshot` row. Snapshots are write-once; the schema has no update path.
- **get_snapshots_for_audit() / get_snapshots_for_object()** — read paths used by the hydration layer when a client requests a resolved audit entry.

## Row Deserializers

Four private `_row_to_*` methods convert raw `aiosqlite` row tuples into Pydantic models. JSON columns (patches, context, parameters) are deserialized inline. These are called by every read method, centralizing the mapping logic.

## Known Gaps

- No database migration system; adding columns requires manual SQLite `ALTER TABLE` or a fresh database.
- `_conn()` opens a new connection on each call wrapped in an `async with` block — no connection pool. For high-throughput workloads this is a bottleneck, though SQLite's WAL mode mitigates some of the serialization penalty.
- `export_audit()` loads all rows for a pocket into memory before serializing — no streaming for large audit logs.