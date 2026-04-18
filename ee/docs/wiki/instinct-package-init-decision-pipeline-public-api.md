---
{
  "title": "Instinct Package Init: Decision Pipeline Public API",
  "summary": "Package init for the Instinct decision pipeline that re-exports all action models (Action, ActionStatus, ActionCategory, ActionPriority, ActionTrigger, ActionContext), audit types (AuditCategory, AuditEntry), and the InstinctStore. Instinct implements the human-in-the-loop decision loop: agent proposes, human approves, action executes, feedback captured.",
  "concepts": [
    "Instinct",
    "decision pipeline",
    "human-in-the-loop",
    "Action",
    "AuditEntry",
    "InstinctStore",
    "approval workflow"
  ],
  "categories": [
    "instinct",
    "decision pipeline",
    "package structure",
    "human-in-the-loop"
  ],
  "source_docs": [
    "f19d09456c361c0d"
  ],
  "backlinks": null,
  "word_count": 186,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Instinct Package Init: Decision Pipeline Public API

## Purpose

This init file defines the public API for the Instinct subsystem — PocketPaw's decision pipeline that puts humans in the loop for agent-proposed actions. The explicit `__all__` list controls the module's contract.

## What Instinct Is

Instinct implements a four-phase decision loop:
1. **Agent proposes** — An AI agent or automation suggests an action
2. **Human approves** — A human reviews and approves/rejects
3. **Action executes** — Approved actions are carried out
4. **Feedback captured** — Results are logged in the audit trail

This ensures agents can't take consequential actions without human oversight.

## Exports

### Action Models
- **Action** — A proposed action awaiting approval
- **ActionStatus** — Lifecycle states (pending/approved/rejected/executed/failed)
- **ActionCategory** — Classification (data/alert/workflow/config/external)
- **ActionPriority** — Urgency levels (low/medium/high/critical)
- **ActionTrigger** — What initiated the action (agent, automation, user, connector)
- **ActionContext** — Supporting data (object IDs, metrics, connector data)

### Audit Models
- **AuditCategory** — Audit event classification (decision/data/config/security)
- **AuditEntry** — Immutable audit log entry

### Store
- **InstinctStore** — Async SQLite persistence for the pipeline

## Known Gaps

None.
