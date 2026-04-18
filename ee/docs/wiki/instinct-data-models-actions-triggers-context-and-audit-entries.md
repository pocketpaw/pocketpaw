---
{
  "title": "Instinct Data Models: Actions, Triggers, Context, and Audit Entries",
  "summary": "Pydantic models and StrEnum types defining the Instinct decision pipeline's data structures. Covers the full action lifecycle from proposal through execution/failure, plus audit entries that create an immutable compliance log of every decision made.",
  "concepts": [
    "Action",
    "ActionStatus",
    "ActionPriority",
    "ActionCategory",
    "ActionTrigger",
    "ActionContext",
    "AuditEntry",
    "AuditCategory",
    "StrEnum",
    "decision lifecycle"
  ],
  "categories": [
    "instinct",
    "decision pipeline",
    "data modeling",
    "Pydantic models",
    "audit"
  ],
  "source_docs": [
    "a918886971e0812b"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Instinct Data Models: Actions, Triggers, Context, and Audit Entries

## Purpose

These models define the data structures for PocketPaw's decision pipeline. They capture the full lifecycle of an agent-proposed action — from initial proposal through human review to execution outcome — and provide an audit trail for compliance and debugging.

## Enums

### ActionStatus (StrEnum)
`pending` -> `approved`/`rejected` -> `executed`/`failed`

The five states form a state machine. `StrEnum` is used (not plain `Enum`) so values serialize directly to JSON-friendly strings without `.value` access.

### ActionPriority
Four levels: `low`, `medium`, `high`, `critical`. Used by the UI to sort and highlight pending actions.

### ActionCategory
Five categories: `data`, `alert`, `workflow`, `config`, `external`. Lets users filter actions by domain — a data team might only care about `data` category actions.

### AuditCategory
Four categories: `decision`, `data`, `config`, `security`. Separate from ActionCategory because audit entries cover broader events (security audits, config changes) beyond just action decisions.

## Core Models

### ActionTrigger
Records what initiated an action: `type` (agent/automation/user/connector) and `source` (specific agent name, rule ID, etc.) plus a human-readable `reason`. This provenance tracking answers "why did this action get proposed?"

### ActionContext
Supporting data for a decision: related `object_ids` (Fabric objects), `connector_data` (raw data from integrations), `metrics` (numerical signals), and free-text `notes`. This gives human reviewers the context they need to approve or reject.

### Action
The central model. Key design decisions:
- **Dual timestamps**: `created_at` (when proposed), `approved_at`, `executed_at` — enables SLA tracking (how long did approval take?)
- **Error tracking**: `error` field captures failure details for `FAILED` status
- **Rejection reason**: `rejected_reason` is separate from `error` — rejections are intentional human decisions, not system failures
- **Pocket scoping**: `pocket_id` ties actions to a specific pocket (workspace unit)

### AuditEntry
Immutable log entries with:
- **Actor format**: `"agent:claude"`, `"user:prakash"`, `"system"` — structured string allows easy filtering by actor type
- **Event taxonomy**: `"action_proposed"`, `"action_approved"`, etc. — machine-readable event types
- **Optional AI recommendation**: Preserves what the AI recommended, enabling analysis of AI decision quality over time

## Cross-Module Dependency

Uses `_gen_id` from `ee.fabric.models` for ID generation — Instinct depends on Fabric's ID scheme to maintain consistent ID formats across subsystems.

## Known Gaps

- `Action` uses `datetime.now()` (local time) for defaults rather than `datetime.now(UTC)` — potential timezone issues.
- No state machine enforcement — nothing prevents setting an Action's status to `executed` without going through `approved` first. Enforcement happens at the store level.
- `ActionContext.object_ids` references Fabric object IDs but there's no validation that those objects exist.
