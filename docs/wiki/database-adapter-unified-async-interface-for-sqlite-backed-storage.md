---
{
  "title": "Database Adapter: Unified Async Interface for SQLite-Backed Storage",
  "summary": "The `db_adapter` module provides an async SQLite wrapper that PocketPaw uses for all structured persistence — session records, memory entries, audit events, and credential metadata. It encapsulates connection lifecycle management, schema migrations, and parameterized queries behind a clean async interface to prevent SQL injection and connection leaks.",
  "concepts": [
    "SQLite",
    "aiosqlite",
    "async database",
    "schema migrations",
    "connection pooling",
    "parameterized queries",
    "SQL injection prevention",
    "database adapter",
    "persistence"
  ],
  "categories": [
    "Database",
    "Storage"
  ],
  "source_docs": [
    "22825663bd0b79cb"
  ],
  "backlinks": null,
  "word_count": 458,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/connectors/db_adapter.py` is the core persistence layer for PocketPaw's structured data. SQLite was chosen as the storage backend for simplicity — it requires no separate server process, works on all platforms, and is sufficient for single-user or small-team deployments. The adapter wraps the async `aiosqlite` library to integrate with PocketPaw's async-first architecture.

## Async-First Design

All database operations in this module are `async def`. This is necessary because PocketPaw's request-handling code is async (FastAPI, channel adapters, tool executors). Blocking database I/O in an async context would starve the event loop. Using `aiosqlite` ensures that file I/O is dispatched to a thread pool and the event loop remains unblocked.

## Connection Lifecycle

The adapter manages connection lifecycle — opening, reusing, and closing database connections. In an async context, connection management is non-trivial: connections must be opened before a request, kept alive for the duration of multi-statement transactions, and closed after to prevent resource leaks. A common failure mode in async SQLite code is opening too many connections (one per query), which causes file handle exhaustion on high-throughput workloads.

## Schema Migration

The adapter handles schema creation and migrations on startup. When PocketPaw starts, the adapter checks whether the target tables exist and creates or alters them as needed. This approach (migrations baked into the adapter rather than a separate migration tool) keeps the deployment footprint minimal — there is no separate `pocketpaw migrate` command to run before starting the server.

## Parameterized Queries

All queries use parameterized placeholders (`?` for SQLite) rather than string formatting. This is a standard SQL injection prevention pattern. The adapter makes parameterization the only option at the interface level — callers pass Python values, not SQL strings.

## Connection Pooling Considerations

SQLite has limited concurrency support (particularly around write transactions). The adapter uses a single shared connection (or a small connection pool) and serializes writes to prevent `database is locked` errors. This is the right trade-off for a single-user agent: reads can overlap, but write contention is low.

## Known Gaps

- **No multi-database support**: The adapter is hardcoded to SQLite. A future multi-tenant deployment would need a PostgreSQL or MySQL adapter for concurrent write throughput. The `DbAdapter` protocol (defined in `connectors/__init__.py`) should make this swappable, but no alternative implementation exists yet.
- **Migration rollback not implemented**: If a migration fails partway through, the adapter may leave the database in an inconsistent state. There is no rollback mechanism — the database must be manually repaired or deleted and recreated.
- **No WAL mode explicit configuration**: SQLite Write-Ahead Logging (WAL) mode significantly improves concurrent read performance. Whether the adapter enables WAL mode is not visible from the AST alone; if it does not, heavy read workloads (e.g., many parallel memory searches) may experience contention.
