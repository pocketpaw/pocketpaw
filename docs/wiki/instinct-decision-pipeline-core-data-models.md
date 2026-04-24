---
{
  "title": "Instinct Decision Pipeline — Core Data Models",
  "summary": "Defines the Pydantic data models and enumerations that underpin the Instinct decision pipeline: the proposed action, its lifecycle states, audit entries, and the contextual metadata that connects an action to its triggering source. These types are the stable contract shared by the store, router, correction system, and any agent that proposes actions.",
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
    "Pydantic BaseModel",
    "_gen_id",
    "PENDING state",
    "lifecycle state machine"
  ],
  "categories": [
    "instinct engine",
    "data models",
    "enterprise edition",
    "audit and compliance"
  ],
  "source_docs": [
    "7555ed6c0a2cbf1a"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`instinct/models.py` is the schema layer for the Instinct decision pipeline. Every other module in `ee.instinct` depends on these types but none of them depend on each other through this file, keeping the dependency graph clean.

## Enumerations

### ActionStatus
Tracks the lifecycle of a proposed action through five states: `PENDING` → (`APPROVED` | `REJECTED`) → (`EXECUTED` | `FAILED`). Using `StrEnum` (Python 3.11+) means instances serialize to plain strings in JSON responses and SQLite rows without a custom encoder, and comparison against string literals in query filters works directly.

### ActionPriority
Four levels: `LOW`, `MEDIUM` (default on `Action`), `HIGH`, `CRITICAL`. The priority is set by the agent at proposal time and can be corrected by the human — the correction loop tracks `priority` as one of the five correctable scalar fields.

### ActionCategory
Five categories: `DATA`, `ALERT`, `WORKFLOW` (default), `CONFIG`, `EXTERNAL`. These map to the kinds of actions an agent can propose across Paw OS connectors. The category drives which audit log queries are meaningful — a compliance team might query only `CONFIG` and `EXTERNAL` category actions.

### AuditCategory
A separate enum for audit log entries (not actions), covering operational events beyond the action lifecycle itself.

## Core Models

### ActionTrigger
Captures what caused the agent to propose the action: `type` (one of `"agent"`, `"automation"`, `"user"`, `"connector"`), `source` (the identifier of the triggering entity), and `reason` (a human-readable explanation). This triad makes the audit log answer "why did this happen" independently of the action content.

### ActionContext
Optional supporting data attached to a proposal: `object_ids` (Fabric object references the action targets), `connector_data` (raw payload from the triggering connector), `metrics` (numeric signals that informed the decision), and `notes` (free text). All fields default to empty so callers do not need to construct context for simple proposals.

### Action
The central model. Key design points:
- `id` is generated with `_gen_id("act")` producing a prefixed identifier (`act_...`) for log readability and foreign-key tracing.
- `status` defaults to `PENDING` — every action starts awaiting human review.
- `priority` defaults to `MEDIUM` — agents should override this based on reasoning, and the correction loop will learn if they consistently get it wrong.
- `context` and `parameters` default to empty/factory values so agents that do not need rich context do not have to construct boilerplate.

### AuditEntry
The immutable log record written after every state transition. Holds `actor`, `event` (a string like `"action.approved"`), `description`, optional `action_id` and `pocket_id`, `category`, and rich context fields including `ai_recommendation` and `outcome`. Once written, audit entries are never mutated.

## Known Gaps

- There is no `version` field on `Action`; if the schema evolves, existing rows in SQLite will not carry a migration marker.
- `ActionContext.connector_data` is typed as `dict[str, Any]` with no size constraint at the model layer — large payloads from connectors are only bounded at the store's SQLite serialization step.