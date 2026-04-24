---
{
  "title": "Paw Print Store — Async SQLite Persistence for Widgets and Events",
  "summary": "Provides all async SQLite read and write operations for the Paw Print widget layer, including widget CRUD with token rotation, append-only event recording, and a dual-level rate-limit enforcement method that checks both overall widget throughput and per-customer throughput within a rolling time window.",
  "concepts": [
    "PawPrintStore",
    "aiosqlite",
    "widget CRUD",
    "access token rotation",
    "rate limiting",
    "within_rate_limit",
    "per-customer rate limit",
    "rolling time window",
    "append-only event log",
    "SCHEMA_SQL",
    "customer_ref"
  ],
  "categories": [
    "paw print",
    "data persistence",
    "SQLite",
    "enterprise edition"
  ],
  "source_docs": [
    "464ca9f70af8b081"
  ],
  "backlinks": null,
  "word_count": 516,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`PawPrintStore` is an `aiosqlite`-backed store managing two tables: `paw_print_widgets` (widget configuration) and `paw_print_events` (append-only customer interaction log). The structure mirrors `InstinctStore` so developers familiar with the decision pipeline persistence pattern can read this store without learning a new pattern.

## Schema

The `SCHEMA_SQL` constant defines both tables with `CREATE TABLE IF NOT EXISTS`, making schema application idempotent. The widgets table stores `spec` and `event_mapping` as JSON text columns. The events table is append-only — there is no update or delete path for events, which is intentional: the event log is the source of truth for customer interaction history and must be immutable for audit purposes.

## Widget CRUD

- **create_widget()** — inserts a new widget row. The `access_token` is already set on the `PawPrintWidget` object by the model's `_gen_token()` factory before the store sees it.
- **list_widgets()** — returns widgets filtered by `pocket_id` and optionally by `owner`, with a configurable `limit`. Used by the management dashboard.
- **delete_widget()** — removes the widget row. Returns `True` if a row was deleted, `False` if the widget did not exist. This boolean return lets the router distinguish 404 from a successful delete cleanly.

Token rotation: the store does not implement token rotation directly — the router generates a new `_gen_token()` value and calls an update path. The existing `access_token` column is simply overwritten, invalidating any cached copies the client holds.

## Event Recording

**record_event()** is a simple insert into `paw_print_events`. The `payload` dict is JSON-serialized. `customer_ref` is the opaque customer identifier forwarded from the widget.js `data-customer` attribute — it is stored but not validated, enabling pseudonymized tracking without PII requirements.

**recent_events()** returns the N most recent events for a widget, used by the router's events list endpoint for widget operators reviewing what their customers have been doing.

## Rate Limiting — within_rate_limit

This is the most complex method. It runs two counts against a rolling time window:

1. **Overall rate** — counts all events for the widget in the last minute. Rejects if `>= overall_per_min`.
2. **Per-customer rate** — counts events for the specific `customer_ref` in the last minute. Rejects if `>= per_customer_per_min`.

Both checks run as SQL `COUNT` queries against the events table with a `created_at >= ?` filter. The `now` parameter is injectable for deterministic testing — callers pass `datetime.utcnow()` in production and a fixed time in tests.

**Why SQL-based rate limiting?** For a single-node deployment, counting rows in SQLite is fast enough and requires no additional infrastructure (no Redis, no in-memory counter). The tradeoff is that it does not work across multiple workers sharing the same SQLite file — though WAL mode handles the concurrent reads, the count window could be slightly inaccurate under high concurrency.

## Row Deserializers

`_row_to_widget()` and `_row_to_event()` map raw `aiosqlite` row tuples to Pydantic models, with JSON deserialization for `spec` and `payload` columns.

## Known Gaps

- SQL-based rate limiting is approximate under multi-worker deployments; a Redis-backed counter would be needed for strict enforcement.
- No bulk event query or pagination on `recent_events()` — only a limit-based head read, which is insufficient for large-volume widgets with high customer traffic.