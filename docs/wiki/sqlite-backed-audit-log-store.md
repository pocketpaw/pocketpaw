---
{
  "title": "SQLite-Backed Audit Log Store",
  "summary": "Implements the `AuditStore` class that persists audit entries to a SQLite database using the stdlib `sqlite3` module with async-friendly execution via `run_in_executor`. Provides filtered querying, full-text search with injection-safe LIKE escaping, CSV/JSON export, and convenience methods for common audit event types.",
  "concepts": [
    "AuditStore",
    "SQLite",
    "run_in_executor",
    "LIKE escaping",
    "_fts_escape",
    "full-text search",
    "export CSV",
    "export JSON",
    "singleton pattern",
    "compliance storage"
  ],
  "categories": [
    "audit",
    "storage",
    "compliance",
    "security"
  ],
  "source_docs": [
    "f200b25905eba38e"
  ],
  "backlinks": null,
  "word_count": 563,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## SQLite-Backed Audit Log Store

The `AuditStore` class is the persistence layer for PocketPaw's enterprise audit system. It stores `AuditEntry` records in a SQLite database, chosen deliberately for its zero-dependency operation — no external database server, no connection pool management, no migration framework needed. The entire audit history lives in a single file adjacent to the pocket database.

### Schema Design

The DDL creates one table (`audit_log`) and four indexes:

```python
_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    pocket_id TEXT,
    ...
);
CREATE INDEX IF NOT EXISTS idx_audit_pocket    ON audit_log(pocket_id);
CREATE INDEX IF NOT EXISTS idx_audit_category  ON audit_log(category);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_actor     ON audit_log(actor);
"""
```

The four indexes cover the most common query patterns: filtering by pocket, filtering by category (for compliance reports), sorting by time (for dashboards), and filtering by actor (for user activity reviews). Without these indexes, queries over a large audit log would degrade to full table scans.

All timestamps are stored as ISO 8601 strings rather than Unix integers. This makes the raw SQLite database human-readable without a viewer tool — a compliance auditor can open the database directly and understand the data.

### Async Interface via run_in_executor

SQLite operations are blocking by nature. The `AuditStore` wraps all database calls in `asyncio.get_event_loop().run_in_executor(None, ...)` to offload the blocking I/O to a thread pool without blocking the FastAPI event loop. This is the standard pattern for integrating synchronous I/O libraries into async Python applications.

### Full-Text Search with LIKE Escaping

The `_fts_escape` function is a critical security component. Before a search term is used in a LIKE clause, it escapes three characters: `\` (to `\\`), `%` (to `\%`), and `_` (to `\_`). The escape order matters — the backslash must be escaped first, otherwise the subsequent escapes would double-escape. The resulting term is passed as a bound parameter, never concatenated. The docstring explicitly calls out the failure mode: a search for `admin_` would otherwise match `admin1`, `admin2`, etc., leaking row existence information.

### Convenience Logging Methods

Beyond the generic `log_entry`, the store provides two specialized methods:

- **`log_tool_execution`** — records when the agent invokes a tool, with the tool name, description, and outcome auto-wired to the `"decision"` category and `metadata`.
- **`log_connector_sync`** — records data connector synchronizations with record count, useful for data lineage audits.

These convenience methods reduce the boilerplate required for the most frequent audit events and ensure consistent field mapping across callers.

### Export Capabilities

`export_csv` and `export_json` return in-memory `bytes` objects rather than writing to files. This keeps the store infrastructure-agnostic — the router layer decides whether to stream the bytes as a file download, write them to object storage, or attach them to an email.

### Singleton Pattern

`get_audit_store()` returns a module-level singleton. This ensures all requests share a single SQLite connection manager, avoiding the overhead of opening and closing connections per request while maintaining thread safety through the executor pattern.

### Known Gaps

The store has no row count limit or archiving strategy. A high-volume PocketPaw deployment (many agent actions per minute) would accumulate millions of rows over months, degrading query performance even with indexes. An archiving job that moves older entries to a compressed JSONL file, or a configurable retention window that deletes entries older than N days, would prevent unbounded growth.