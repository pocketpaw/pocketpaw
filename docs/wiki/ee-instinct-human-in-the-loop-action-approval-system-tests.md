---
{
  "title": "EE Instinct: Human-in-the-Loop Action Approval System Tests",
  "summary": "This test suite validates the full lifecycle of PocketPaw's Instinct subsystem, which gates AI-proposed actions behind human approval before execution. It covers store-layer CRUD, audit trail integrity, REST endpoint contracts, lifecycle state transitions, and edge cases such as approving non-existent actions.",
  "concepts": [
    "human-in-the-loop",
    "action approval",
    "InstinctStore",
    "audit trail",
    "action lifecycle",
    "rejection reason",
    "pending actions",
    "pocket filtering",
    "audit export",
    "FastAPI TestClient",
    "SQLite fixture isolation"
  ],
  "categories": [
    "testing",
    "agent safety",
    "human-in-the-loop",
    "audit",
    "test"
  ],
  "source_docs": [
    "e0de4a78e00c6b4f"
  ],
  "backlinks": null,
  "word_count": 566,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee.instinct` subsystem implements a **human-in-the-loop approval gate** for AI-generated actions. When an AI agent wants to perform a sensitive operation — such as deleting a record, sending a message, or triggering a workflow — it first proposes the action. A human operator then approves or rejects it before execution proceeds. This prevents autonomous agents from taking irreversible actions without oversight.

## Why This Exists

Unguarded AI autonomy in production systems creates risk. Without an approval layer, a misbehaving or misguided agent could modify data, send communications, or trigger integrations that cannot be undone. The Instinct module enforces a mandatory review step, making it safe to wire agents into production workflows.

## Test Structure

The test file is organized into two layers:

**Store-level tests** (`TestProposeAction`, `TestApproveAction`, `TestRejectAction`, `TestListPending`, `TestListActionsByStatus`, `TestQueryAudit`, `TestExportAudit`) operate directly on `InstinctStore`, an isolated SQLite-backed store created fresh per test via the `store(tmp_path)` fixture. This ensures no cross-test contamination.

**Router/endpoint tests** (`TestProposeActionEndpoint`, `TestListPendingEndpoint`, `TestApproveEndpoint`, `TestRejectEndpoint`, `TestAuditEndpoint`, `TestAuditExportEndpoint`, `TestApproveNonexistentEndpoint`, `TestFullLifecycle`) exercise the FastAPI router using a `TestClient`. The `client` fixture patches the store singleton to use the isolated `router_store`, ensuring HTTP tests also run against ephemeral state.

## Key Behaviors Pinned

**Action lifecycle:** Every action starts in `pending` status. `TestApproveAction` and `TestRejectAction` verify that status transitions are irreversible and that each transition writes an audit entry. The audit trail is the durable record of who did what and when.

**Rejection reasons:** `TestRejectActionWithReason` confirms that rejection reasons persist round-trip through the store and are retrievable via `get_action`. `test_rejection_without_reason_stores_empty` ensures the field doesn't raise when omitted — empty string is the canonical empty value, not `None`.

**Non-existent ID handling:** `TestApproveNonexistent` tests that approving or rejecting an unknown action ID returns `None` rather than raising an exception. This matters because a concurrent rejection between an approval attempt and the DB write would otherwise produce an unhandled error. The endpoint tests (`TestApproveNonexistentEndpoint`) verify this surfaces as HTTP 404, not 500.

**Filtering and isolation:** `TestListPending` verifies that `list_pending` excludes approved/rejected actions and can filter by `pocket_id`. This ensures a multi-agent environment doesn't leak pending actions across pocket boundaries.

**Audit export:** `TestExportAudit` confirms that the export endpoint returns all entries as a valid JSON attachment, filterable by pocket. This supports compliance and debugging workflows.

**Category system:** Actions carry a category (`workflow`, `security`, `data`). `TestQueryAuditByCategory` locks in that audit queries can filter by category, which enables category-specific review dashboards.

## Fixtures

- `store(tmp_path)` — Creates a fresh `InstinctStore` backed by a temp SQLite file. Never touches `~/.pocketpaw`.
- `test_app(tmp_path)` — Builds a FastAPI app with the instinct router and patches the store singleton.
- `client(test_app, router_store)` — `TestClient` with `_store` patched to the isolated `router_store`.
- `make_trigger(source, type_)` — Helper returning a minimal `ActionTrigger` for test payloads.

## Endpoint Contracts

- `POST /instinct/actions` → 201 with action object; 422 on missing required fields.
- `GET /instinct/actions/pending` → list of pending actions, filterable by `pocket_id`.
- `GET /instinct/actions` → paginated list with `total`, filterable by status.
- `POST /instinct/actions/{id}/approve` → approved action; 404 if unknown.
- `POST /instinct/actions/{id}/reject` → rejected action with optional reason body.
- `GET /instinct/audit` → audit entries with `total`, filterable by pocket, event, category.
- `GET /instinct/audit/export` → JSON file attachment of all audit entries.

## Known Gaps

No known TODOs or FIXMEs appear in this test file. The lifecycle is fully covered including the empty-reason edge case, multi-action pending filtering, and concurrent non-existent ID handling.
