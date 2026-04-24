---
{
  "title": "Runtime Audit API Integration Tests — Workspace Rollup, Search, CSV Export, and Security",
  "summary": "This test module validates the canonical `/runtime/audit` endpoint introduced in Cluster C/PR4, covering workspace-scoped filtering, full-text search with length caps, CSV export, SQL injection resistance, and the deprecation forwarding behavior of the legacy `/audit` alias. It exercises both the new runtime router and the legacy router mounted together to prove they share the same underlying AuditStore and scope guard.",
  "concepts": [
    "AuditStore",
    "runtime audit endpoint",
    "workspace rollup",
    "full-text search",
    "SQL injection prevention",
    "q length cap",
    "deprecation header",
    "CSV export",
    "scope guard override",
    "pytest fixture",
    "FastAPI dependency injection",
    "multi-tenant isolation"
  ],
  "categories": [
    "testing",
    "audit and compliance",
    "API",
    "security",
    "test"
  ],
  "source_docs": [
    "c9ff80d7a0b2e5f0"
  ],
  "backlinks": null,
  "word_count": 650,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_api_v1_runtime_audit.py` is the integration test suite for PocketPaw's canonical audit surface, `/api/v1/runtime/audit`, and its legacy predecessor `/api/v1/audit`. Written during the Cluster C / PR4 audit overhaul (April 2026), the file exists because the audit system was restructured: the old flat `/audit` route was superseded by a richer `/runtime/audit` route that understands workspace boundaries and structured filtering. The test suite exists to prove the migration is correct and backward-compatible.

## Fixture Architecture

**`store`** — creates a real `AuditStore` backed by a SQLite file in pytest's ephemeral `tmp_path`. Using a real store (rather than a mock) is deliberate: the workspace rollup and full-text search logic lives inside `AuditStore.query`, and mocking it would hide SQL bugs. The tradeoff is that tests are slightly slower, but the correctness guarantee is much stronger.

**`client`** — builds a minimal `FastAPI` app with both routers mounted and then applies two dependency overrides:
1. `get_audit_store` is replaced with a lambda returning the fixture store, so the router operates on isolated test data.
2. `require_scope("audit")` is replaced with a no-op lambda so tests do not have to manufacture authentication tokens. The comment explains that both the legacy and runtime routers derive from the same `require_scope` factory call, so a single override disables auth on both.

**`seed`** — inserts three known audit entries across two workspaces (`ws-alpha` with two entries, `ws-beta` with one) before each test that needs populated data.

## Test Coverage

### `test_runtime_audit_workspace_rollup`
Queries `?workspace_id=ws-alpha` and asserts exactly two entries are returned, each with `context.workspace_id == "ws-alpha"`. This prevents the regression where a flat `SELECT *` would return entries from all tenants, leaking cross-workspace audit data — a serious multi-tenant isolation failure.

### `test_runtime_audit_full_text_search`
Queries `?q=pipeline` and asserts only the one matching entry is returned. The search is expected to cover both `description` and `context` fields. This test ensures the `q` parameter does meaningful filtering rather than being silently ignored.

### `test_runtime_audit_q_length_cap`
Sends a 201-character query (one over the 200-character Pydantic `max_length` limit) and expects HTTP 422. The cap exists because the query is used in a `LIKE '%...%'` SQL clause. Without a length cap, an attacker could send a very long string to exhaust CPU on the database's pattern-matching engine. The 422 response proves the validation runs before the query reaches SQLite.

### `test_legacy_audit_forwards_and_deprecation_header`
Calls `/api/v1/audit` (the old path) and asserts:
- HTTP 200 with all three rows (no workspace or text filter applied on the legacy path).
- `Deprecation: true` response header.
- A `Link` header pointing to `/runtime/audit`.

This test protects existing clients that have not yet migrated: the legacy endpoint must keep working while simultaneously signaling that they should update their integration.

### `test_runtime_audit_export_csv`
Calls `/runtime/audit/export?format=csv&workspace_id=ws-alpha` and verifies the `Content-Type` is `text/csv` and the body contains at least three newlines (one header row plus two data rows). This covers the export code path that the JSON tests do not exercise.

### `test_runtime_audit_rejects_injection_q`
Sends `?q='; DROP TABLE audit_log; --` and expects HTTP 200 with zero results — and then re-queries without a filter to confirm all three seeded rows still exist. The two-step assertion is the critical part: the first check shows the injection was not interpreted as data; the second proves the table itself was not corrupted. If the query parameter were interpolated directly into SQL (rather than parameterized), the `DROP TABLE` would succeed and the follow-up check would return 0, catching the vulnerability.

## Known Gaps

No TODOs or FIXMEs appear in the source. However:
- **Pagination** is not tested — the export and listing endpoints may have undocumented page limits that are exercised only by manual testing.
- **Category and actor filters** visible in the seed data are not validated as query parameters, suggesting those filter axes may not be implemented or are untested.
- The `test_runtime_audit_q_length_cap` relies on Pydantic validation raising 422; if the route signature changes to accept a plain `str` instead of `Query(max_length=200)`, the cap disappears silently.
