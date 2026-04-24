---
{
  "title": "Audit FTS Security Tests: SQL Injection Prevention and LIKE Wildcard Escaping",
  "summary": "Security regression tests proving that the `q` parameter on PocketPaw's `/runtime/audit` search endpoint is fully parameter-bound and cannot corrupt or drop the `audit_log` table, and that LIKE special characters (`%`, `_`) in the query are correctly escaped to prevent wildcard-widened result sets. Tests correspond to Cluster C PR4.",
  "concepts": [
    "SQL injection prevention",
    "FTS security",
    "parameter binding",
    "LIKE wildcard escaping",
    "_fts_escape",
    "audit search",
    "DROP TABLE prevention",
    "case-insensitive search",
    "multi-field FTS",
    "workspace ID search"
  ],
  "categories": [
    "testing",
    "security",
    "audit",
    "SQL injection",
    "test"
  ],
  "source_docs": [
    "cda25f9688b8306a"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_audit_fts_security.py` is a narrowly focused security regression test file for the full-text search (FTS) capability of PocketPaw's audit store. The tests prove two distinct classes of attack against the audit search endpoint are neutralized.

The file was created as part of Cluster C / PR4 (2026-04-19) after a review identified that the `q` parameter path needed explicit security validation.

## SQL Injection Prevention

`test_injection_cannot_drop_table` seeds two legitimate audit rows, then passes a classic SQL injection string as the search query:

```python
evil = "'; DROP TABLE audit_log; --"
results = await store.search_entries(q=evil)
```

The test asserts:
1. `results == []` — the injection string doesn't match any row (correct — the literal substring does not exist in the data).
2. `len(all_rows) == 2` — the `audit_log` table still has its two rows after the injection attempt.

**Why parameter binding matters:** SQLite FTS queries built with string interpolation (e.g., `f"WHERE ... MATCH '{q}'"`) would execute the injected SQL literally. Parameterized queries treat the entire `q` value as a string literal, making injection structurally impossible. This test proves the parameterized approach is in place and cannot be accidentally reverted.

## LIKE Wildcard Escaping

`test_wildcard_inputs_are_escaped` seeds a row with a description containing a literal underscore (`admin_user`) and queries with `q="admin_"`. Without escaping, `_` in a LIKE pattern matches any single character, so the query would match `adminXuser`, `admin_user`, `admin0user`, etc. — silently widening the result set beyond the user's intent.

The test asserts that `admin_` matches only rows with a literal underscore at that position, not any character.

`_fts_escape` is the utility function responsible for escaping `%` and `_` before they enter the LIKE/MATCH expression. `test_fts_escape_unit` tests this function directly as a unit test.

## Case-Insensitive Search

`test_search_is_case_insensitive` verifies that `q="ALICE"` matches rows containing `"alice"`. Audit search should be case-insensitive to support natural user queries without requiring exact case knowledge.

## Multi-Field Search Span

`test_search_spans_action_description_context` seeds rows with the search term in different fields (`action`, `description`, `context`) and verifies all are found by a single query. FTS that only scanned one column would miss events recorded in other fields, making audit investigations incomplete.

## Workspace ID Context Matching

`test_workspace_id_matches_context_field` verifies that a workspace ID stored in the `context` JSON field is discoverable via FTS. This enables operators to search for all audit events related to a specific workspace by ID.

## Known Gaps

No TODO or FIXME markers. The tests cover SQL injection via string literals and LIKE wildcards but do not cover:
- FTS operator injection (e.g., using SQLite FTS5 operators like `OR`, `AND`, `NOT` in the query).
- Very long query strings that might cause performance issues or internal buffer overflows.
- Unicode normalization edge cases in search matching.