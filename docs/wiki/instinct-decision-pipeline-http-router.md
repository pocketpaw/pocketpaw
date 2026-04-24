---
{
  "title": "Instinct Decision Pipeline — HTTP Router",
  "summary": "Provides the FastAPI HTTP surface for the Instinct decision pipeline, handling action proposals, approvals with optional human edits, rejections, and corrections listing. It enforces that soul bridge failures never block an approval and that edit diffs are captured and stored before the status transition.",
  "concepts": [
    "propose action",
    "approve action",
    "reject action",
    "best-effort soul bridge",
    "correction diff",
    "persist edits before approval",
    "reasoning trace attachment",
    "HydratedAuditEntry",
    "Why drawer",
    "audit export",
    "corrections endpoint"
  ],
  "categories": [
    "instinct engine",
    "REST API",
    "decision pipeline",
    "enterprise edition"
  ],
  "source_docs": [
    "eb5d15bf5a21637b"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Instinct router is the HTTP entry point for the full propose → approve/reject → audit cycle. It owns the sequencing logic that glues together `InstinctStore`, the correction diff system, and the soul bridge, ensuring each step happens in the right order with safe failure handling.

## Endpoints

### POST /instinct/actions — propose_action

Accepts a `ProposeRequest` containing the action fields plus an optional `reasoning_trace` and `fabric_snapshots`. The trace and snapshots let the calling agent attach the full context of its reasoning — which fabric objects it queried, which soul memories it recalled, which tools it called — at proposal time. This data is stored alongside the action and surfaced in the audit log's "Why?" drawer.

### POST /instinct/actions/{action_id}/approve — approve_action

This is the most complex endpoint. The `ApproveRequest` body may carry optional edited versions of `title`, `description`, and `recommendation`. The handler:

1. Loads the current action from the store.
2. If any edited fields differ, calls `_persist_edits()` to update the stored action before the status transition — ensuring the approval is against the human's intended version, not the original.
3. Constructs an `Action` snapshot from the edits and calls `compute_patches(before, after)`.
4. Builds a `Correction` and records it in the store.
5. Calls `approve()` on the store to transition status to `APPROVED`.
6. Calls `_forward_to_soul()` in a best-effort fire-and-forget manner.

Step 2 happening before step 5 is critical: if the edit persistence failed but the approval succeeded, the stored action would not reflect what the human approved.

### _forward_to_soul — Best-Effort Soul Bridge

`_forward_to_soul(correction, action)` wraps the `CorrectionSoulBridge.record()` call in a try/except that logs failures at WARNING level and returns without raising. The explicit design decision: soul integration is an enhancement. An approval must succeed even if the soul is down, misconfigured, or throws an unexpected exception. Breaking an approval flow over a learning subsystem failure would be unacceptable in production.

### POST /instinct/actions/{action_id}/reject — reject_action

Accepts an optional `RejectRequest.reason` string, transitions the action to `REJECTED`, and writes an audit entry. Rejection does not produce a `Correction` because there is no approved version to diff against.

### GET /instinct/actions — list_actions

Returns all actions for a pocket, optionally filtered by status. Used by the dashboard's pending-actions panel and by agents querying what has been previously approved or rejected.

### GET /instinct/audit and GET /instinct/audit/export

Query and export the audit log. The `HydratedAuditEntry` model expands referenced IDs for the Why? drawer UI, resolving `reasoning_trace` and `fabric_snapshots` inline.

### GET /instinct/corrections

Returns corrections scoped to a pocket or a specific action, enabling both the UI and agents to read the full edit history.

## Known Gaps

- `_store()` uses a module-level late import (`from ee.api import get_instinct_store`) to avoid circular imports at load time — this is a HACK that should be replaced with proper dependency injection.
- The `HydratedAuditEntry` hydration is done in the router layer rather than in a dedicated service, mixing I/O with response shaping.