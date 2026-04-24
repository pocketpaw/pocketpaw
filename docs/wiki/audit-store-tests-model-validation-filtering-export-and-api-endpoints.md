---
{
  "title": "Audit Store Tests: Model Validation, Filtering, Export, and API Endpoints",
  "summary": "Comprehensive tests for PocketPaw's `AuditStore` — the SQLite-backed audit trail that records all security-relevant operations. Tests cover the `AuditEntry` model, log persistence, multi-field query filtering, date range filtering, CSV and JSON export, REST API endpoints, and integration helpers for tool execution and connector sync logging.",
  "concepts": [
    "AuditStore",
    "AuditEntry",
    "audit trail",
    "SQLite",
    "JSONL",
    "CSV export",
    "JSON export",
    "pocket_id filter",
    "date range filter",
    "log_tool_execution",
    "log_connector_sync",
    "compliance logging"
  ],
  "categories": [
    "testing",
    "audit",
    "compliance",
    "data persistence",
    "test"
  ],
  "source_docs": [
    "57875ea30bb6cec9"
  ],
  "backlinks": null,
  "word_count": 491,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_audit.py` is the primary test file for PocketPaw's audit subsystem. The audit system provides a tamper-evident log of all significant agent actions, authentication events, tool executions, and data operations. This log is queried by the dashboard's audit viewer and exported for compliance reporting.

## AuditEntry Model Validation

`TestAuditEntryModel` tests the Pydantic model that represents a single audit record:

- `test_model_has_required_fields` — verifies `actor`, `action`, `category`, and `description` are required.
- `test_model_defaults` — confirms `id` is auto-generated and `timestamp` defaults to the current UTC time.
- `test_model_id_is_unique` — creates two entries and asserts the IDs differ.
- `test_model_timestamp_is_utc_iso` — verifies the timestamp format is ISO 8601 UTC, which is required for consistent cross-timezone log analysis.
- `test_model_rejects_invalid_status` and `test_model_rejects_invalid_category` — confirm Pydantic raises for values outside the allowed enum sets. Invalid values would produce audit records that don't match the dashboard's filter options.

## AuditStore Log Entry

`TestAuditStoreLogEntry` exercises the `log_entry()` method on an isolated SQLite-backed `AuditStore`:

- `test_log_entry_returns_entry_id` — confirms `log_entry()` returns the assigned ID (callers use this for correlation).
- `test_log_entry_persists_to_db` — verifies the entry actually appears in a subsequent query.
- `test_log_entry_stores_all_fields` — asserts all fields survive the SQLite round-trip intact.
- `test_log_entry_without_optional_fields` — confirms optional fields (`pocket_id`, `context`) default cleanly.

## Query and Filtering

`TestAuditStoreQueryEntries` tests the multi-field filtering capabilities:

- Filter by `pocket_id` — returns only entries for a specific agent pocket.
- Filter by `category` — returns only entries of a given category (e.g., `"decision"`, `"data"`).
- Filter by `actor` — returns entries from a specific actor (user ID or service name).
- Date range filtering — entries within `start`/`end` are returned; entries outside are excluded.
- Sort order — `test_query_returns_entries_newest_first` confirms results are newest-first.
- Limit — confirms the `limit` parameter caps result count.
- Combined filters — multiple filters applied together narrow results correctly.

## Export

`TestAuditStoreExport` tests data export for compliance and reporting:

- CSV export returns bytes with a correct header row and a row per entry.
- CSV export respects `pocket_id` filter.
- JSON export returns a list of entry dicts.
- JSON export respects `pocket_id` filter.

## REST API

`TestAuditAPIQuery` mounts the audit router on a test app and verifies the HTTP query endpoint:

- Empty store returns an empty list (not a 404).
- Populated store returns entries.
- `pocket_id` and `category` query params filter correctly.
- Each entry in the response has the expected shape.

`TestAuditAPIExport` tests the export endpoint:

- CSV export returns `text/csv` content type.
- JSON export returns `application/json`.
- CSV respects `pocket_id` filter.
- Invalid `format` value returns 400.

## Integration Helpers

`TestAuditIntegration` tests the high-level convenience functions:

- `log_tool_execution` — logs a tool action with correct fields filled in automatically.
- `log_connector_sync` — logs a data connector synchronization event.

## Known Gaps

No TODO or FIXME markers. Tests do not cover audit log rotation under write load or the behavior when the SQLite database file is locked by a concurrent writer.