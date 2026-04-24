---
{
  "title": "Canonical Runtime Audit Router with Full-Text Search",
  "summary": "The authoritative audit query endpoint at `/api/v1/runtime/audit`, consolidating two legacy paths behind a single surface that adds workspace-level rollup filtering and bound-parameter full-text search. SQL injection is explicitly prevented by using parameterized LIKE queries, verified by a dedicated regression test.",
  "concepts": [
    "runtime audit",
    "full-text search",
    "workspace rollup",
    "SQL injection prevention",
    "bound parameters",
    "LIKE escaping",
    "_fts_escape",
    "audit scope",
    "legacy alias",
    "regression test"
  ],
  "categories": [
    "audit",
    "compliance",
    "security",
    "api"
  ],
  "source_docs": [
    "f64493e9ab43a0cb"
  ],
  "backlinks": null,
  "word_count": 533,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Canonical Runtime Audit Router with Full-Text Search

The `runtime_router.py` is the result of the Cluster C consolidation (PR4, 2026-04-19). It replaces two functionally similar but separately maintained endpoints (`/api/v1/audit` and `/api/v1/instinct/audit`) with a single canonical surface that adds the missing features both legacy paths lacked.

### Why Consolidate

Having two separate routers for the same data created a maintenance burden: bug fixes had to be applied twice, filter parameters drifted out of sync, and the frontend had to decide which path to call. The `runtime_router` solves this by being the only surface that receives new development. The legacy paths are kept as thin forwarding aliases that call the same `store.search_entries()` method, so existing callers continue to work while the codebase converges.

### New Capabilities

**Workspace-level rollup** — The `workspace_id` parameter allows querying audit entries across all pockets in a workspace simultaneously. Without this, an enterprise dashboard showing the full activity of a team's AI deployment would need to make one query per pocket and merge the results client-side. The workspace rollup pushes this join into the store layer where it can be expressed as a single SQL query.

**Full-text search** — The `q` parameter searches across `description`, `action`, and the `context` JSON payload. This enables compliance officers to search for entries mentioning a specific customer ID, action type, or data element without knowing which pocket or date range to filter on first.

### SQL Injection Prevention

The most important design decision in this file is how `q` is handled. The module docstring explicitly documents the threat model and the countermeasure:

> The `q` search term is interpolated only through bound parameters. We never concatenate user input into SQL — a deliberate test proves that `q="'; DROP TABLE audit_log; --"` returns zero results and leaves the table intact.

The `_fts_escape` helper in `store.py` escapes LIKE wildcards (`%`, `_`, `\`) in the search term, then the escaped term is passed as a bound parameter with `ESCAPE '\\'`. This two-step approach prevents both SQL injection (via bound parameters) and LIKE wildcard abuse (via escaping). The regression test in `tests/test_audit_fts_security.py` is a permanent fixture — it proves the defense works and will catch any future refactoring that accidentally reintroduces concatenation.

### Authentication

The router requires the `audit` scope via `require_scope`. This is a more specific scope than the legacy router used, which inherited a broader permission. The tighter scope allows operators to grant read-only audit access to compliance tooling without granting admin rights.

### Legacy Alias Compatibility

The `queryAudit` function in the enterprise frontend's `runtime/api.ts` hits `/instinct/audit`. By mounting the legacy paths as thin aliases that forward to this canonical handler, the frontend requires no code changes during the migration. The aliases are maintained in the legacy router files and do not appear in `runtime_router.py` itself, keeping the canonical file clean.

### Known Gaps

The workspace rollup implementation details are not fully visible in the extracted snippet. If the rollup is implemented as a missing `pocket_id` filter (returning all entries regardless of pocket), there is a risk that workspace-scoped tokens could read entries from pockets they are not authorized to view. A proper workspace-to-pocket membership check in the store layer would be needed for multi-tenant isolation.