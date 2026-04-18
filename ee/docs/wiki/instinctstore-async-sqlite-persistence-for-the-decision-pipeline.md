---
{
  "title": "InstinctStore: Async SQLite Persistence for the Decision Pipeline",
  "summary": "Async SQLite store managing the full action lifecycle (propose, approve, reject, execute, fail) and an immutable audit log. Every state transition automatically creates an audit entry, ensuring a complete compliance trail. Uses the same lazy-schema and connection-per-operation patterns as FabricStore.",
  "concepts": [
    "InstinctStore",
    "action lifecycle",
    "audit log",
    "state transitions",
    "_update_status",
    "propose",
    "approve",
    "reject",
    "mark_executed",
    "mark_failed",
    "compliance",
    "aiosqlite"
  ],
  "categories": [
    "instinct",
    "decision pipeline",
    "storage",
    "SQLite",
    "audit",
    "async"
  ],
  "source_docs": [
    "2513c911da8f837b"
  ],
  "backlinks": null,
  "word_count": 643,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# InstinctStore: Async SQLite Persistence for the Decision Pipeline

## Purpose

`InstinctStore` is the persistence layer for PocketPaw's decision pipeline. It handles two responsibilities: managing the action lifecycle (state transitions from pending through execution) and maintaining an immutable audit log that records every decision for compliance.

## Schema

Two tables:

### `instinct_actions`
Stores proposed actions with their full lifecycle state. Key columns:
- `trigger` — JSON-serialized ActionTrigger (what initiated the action)
- `parameters` — JSON-serialized action parameters
- `context` — JSON-serialized ActionContext (supporting data)
- Status tracking: `status`, `approved_by`, `approved_at`, `rejected_reason`, `executed_at`, `error`

### `instinct_audit`
Immutable audit log. Every action state transition generates an entry. Key columns:
- `actor` — Structured string like `"agent:claude"` or `"user:prakash"`
- `event` — Machine-readable event type (`action_proposed`, `action_approved`, etc.)
- `category` — Classification for filtering
- `ai_recommendation` — Preserves AI reasoning for later analysis

### Indexes
- `idx_actions_pocket` + `idx_actions_status` — Fast filtering by pocket and status (the most common query patterns)
- `idx_audit_pocket` + `idx_audit_timestamp` — Fast audit queries by pocket and time range

## Lazy Schema Initialization

Same pattern as FabricStore: `_ensure_schema()` with `_initialized` flag and `CREATE TABLE IF NOT EXISTS`. Idempotent and creates the database on first use.

## Action Lifecycle

### propose()
Creates an action in `pending` status and automatically logs an `action_proposed` audit entry. The audit entry captures the AI recommendation, creating a record of what the AI suggested before human review.

### approve() / reject()
Delegate to `_update_status()`, a private method that:
1. Fetches the current action
2. Builds a dynamic UPDATE query from the provided fields
3. Executes the update
4. Logs an audit entry

This shared method prevents duplication across the four state transitions (approve, reject, execute, fail).

### mark_executed() / mark_failed()
Called after an approved action runs. `mark_failed` captures the error message for debugging.

### _update_status() — The State Machine Engine
Accepts arbitrary `**fields` to set on the action row, builds dynamic SQL SET clauses, and auto-logs the transition. The `extra_desc` parameter appends details to the audit description (e.g., rejection reason or error message).

## Audit System

### _log() (private)
Creates an AuditEntry and inserts it. Every state transition in the action lifecycle calls this, ensuring no transition goes unrecorded.

### log() (public)
Exposes audit logging for non-action events — config changes, security events, data operations that aren't part of the action pipeline.

### query_audit()
Dynamic query builder with optional filters for pocket, category, and event type. Returns newest-first (`ORDER BY timestamp DESC`).

### export_audit()
Fetches up to 10,000 entries and serializes to JSON string. Used by the compliance export endpoint.

## Row Conversion

`_row_to_action` and `_row_to_audit` deserialize SQLite rows back into Pydantic models. JSON columns (`trigger`, `parameters`, `context`) are parsed with `model_validate_json` or `json.loads` as appropriate.

## Design Decisions

- **Automatic audit logging**: By coupling audit creation to state transitions inside the store, it's impossible to change an action's status without creating an audit record. This is a deliberate integrity guarantee — the router doesn't need to remember to log.
- **Connection-per-operation**: Same as FabricStore — appropriate for SQLite's concurrency model.
- **No optimistic concurrency**: State transitions don't check the current status before updating. Two concurrent approvals of the same action would both succeed. In practice this is unlikely in a single-user local deployment.

## Known Gaps

- **No state machine validation**: `_update_status` doesn't verify valid transitions (e.g., you could "approve" an already-executed action). Should check `current_status -> new_status` validity.
- **`get_action` is referenced but not shown in the source excerpt**: The `_update_status` method calls `await self.get_action(action_id)` which must exist but wasn't included in the visible source.
- **No concurrent state transition protection**: Two simultaneous approve calls would both succeed without conflict detection.
- **Audit entries reference action IDs but no foreign key enforcement**: Same SQLite PRAGMA issue as FabricStore.
- **`datetime.now()` without timezone**: Timestamps in Python model defaults use local time, while SQLite `datetime('now')` uses UTC.
