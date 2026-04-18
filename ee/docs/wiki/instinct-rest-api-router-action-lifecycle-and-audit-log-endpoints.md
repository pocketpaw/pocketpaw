---
{
  "title": "Instinct REST API Router: Action Lifecycle and Audit Log Endpoints",
  "summary": "FastAPI router for the Instinct decision pipeline, exposing endpoints to propose actions, approve/reject them, list pending and filtered actions, query the audit log, and export audit data as JSON for compliance. Uses a lazy singleton store from the main API module.",
  "concepts": [
    "Instinct router",
    "propose action",
    "approve/reject",
    "audit log",
    "audit export",
    "lazy singleton",
    "deferred import",
    "compliance"
  ],
  "categories": [
    "instinct",
    "decision pipeline",
    "API",
    "REST endpoints",
    "audit"
  ],
  "source_docs": [
    "7cf15df83245d3f9"
  ],
  "backlinks": null,
  "word_count": 403,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Instinct REST API Router: Action Lifecycle and Audit Log Endpoints

## Purpose

This router provides the HTTP interface for the Instinct decision pipeline. It covers the full action lifecycle (propose, approve, reject) and audit log access (query, export).

## Store Access

The `_store()` function uses a deferred import from `ee.api` to get a singleton `InstinctStore`. This avoids circular imports — `ee.api` imports the router, so the router can't import from `ee.api` at module level. The lazy import resolves the dependency at call time.

## Endpoints

### Action Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/instinct/actions` | Propose a new action |
| GET | `/instinct/actions/pending` | List pending actions (optional pocket filter) |
| GET | `/instinct/actions` | List all actions with status/pocket filters |
| POST | `/instinct/actions/{id}/approve` | Approve a pending action |
| POST | `/instinct/actions/{id}/reject` | Reject with optional reason |

### Audit Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/instinct/audit` | Query audit log with filters |
| GET | `/instinct/audit/export` | Download full audit log as JSON file |

## Request/Response Schemas

Defined inline:
- `ProposeRequest` — All fields needed to create an action (pocket_id, title, trigger, etc.)
- `RejectRequest` — Optional reason string
- `ActionsListResponse` — Wraps action list with total count
- `AuditListResponse` — Wraps audit entries with total count

## Design Notes

- **Approve has no request body**: Approval is a simple POST with no additional data — the action ID in the path is sufficient. In the future, `approved_by` should come from an auth context.
- **Reject reason is optional**: Allows quick rejections but encourages explanation.
- **Audit export**: Returns JSON with `Content-Disposition: attachment` header, making it a downloadable file in browsers. Uses a generous 10,000 entry limit.
- **POST for approve/reject**: These are state mutations, not idempotent — POST is correct per REST conventions.

## Known Gaps

- **No authentication**: Like the Fabric router, no auth dependencies. Appropriate for local use but needs guards for multi-user deployment.
- **No pagination on pending actions**: Could become unwieldy if many actions accumulate without review.
- **Approve/reject don't record who did it**: `approver` defaults to `"user"` in the store — should come from authenticated user context.
- **Route ordering**: `/instinct/actions/pending` must be defined before `/instinct/actions/{action_id}` to avoid FastAPI matching "pending" as an action_id. The current order is correct but fragile if routes are reordered.
