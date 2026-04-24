---
{
  "title": "Agent Scope Validation and Assignment Rules",
  "summary": "`scope_rules.py` implements the server-side grammar and authorisation rules for hierarchical scope tags — normalising, validating, and deduplicating scope strings, and checking whether an admin's own scope grant covers the scopes they attempt to assign to an agent.",
  "concepts": [
    "scope grammar",
    "hierarchical scopes",
    "scope validation",
    "ScopeValidationError",
    "FORBIDDEN_SCOPES",
    "normalise_and_validate_scopes",
    "admin_can_assign_scopes",
    "wildcard scopes",
    "ScopePicker",
    "privilege escalation prevention"
  ],
  "categories": [
    "security",
    "agents",
    "validation"
  ],
  "source_docs": [
    "b8b40877fd842049"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Scope Grammar

Scope tags follow a colon-separated hierarchical format that mirrors the frontend `normalise.ts` rules:

- Segments must match `[a-z0-9]+` (lowercase alphanumeric only)
- `*` is only legal as the terminal segment (`org:sales:*` is valid; `org:*:leads` is not)
- Empty segments (`::` or leading `:`) are rejected
- Duplicates in a list are silently collapsed, preserving order

## Why Server-Side Rules Mirror the Frontend

The frontend ScopePicker enforces these rules as a UX layer — it prevents users from typing invalid scopes in the UI. But the REST API can be called directly (curl, CLI, another service). If server-side validation were absent, a direct API call could store a malformed or privilege-escalating scope string that the frontend would then render incorrectly or the agent runtime would misinterpret.

Duplicating the grammar in Python ensures the server is the authoritative validator regardless of how the request arrives.

## The Universal Wildcard Block

```python
FORBIDDEN_SCOPES = frozenset({"*"})
```

The bare `*` scope would grant an agent access to every workspace's data — equivalent to a superadmin token. This is blocked at the grammar level, not just in permission checks, because there is no legitimate UI-driven reason for an agent to hold an unrestricted wildcard. Namespaced wildcards like `org:sales:*` are allowed because they are bounded by at least one namespace segment.

## `normalise_and_validate_scopes()`

The public entry point processes a list of scope strings:

1. Checks each value is a string (not None or a number)
2. Strips whitespace and lowercases
3. Validates against the grammar
4. Deduplicates while preserving order
5. Raises `ScopeValidationError` on the **first** bad tag (fail-fast, so the API response identifies the offender)

`ScopeValidationError` extends `ValueError`, which Pydantic surfaces as a 422 response with a readable `detail` message. This means no explicit error handling is needed at the router level.

## `admin_can_assign_scopes()`

This function checks whether an admin is allowed to assign a given set of scopes to an agent, based on the admin's own configured scopes. The containment rule is hierarchical: `org:sales:*` covers `org:sales` and `org:sales:leads`.

An important current fallback: if `admin_scopes` is `None` or empty (meaning the admin has no explicit scope narrowing), they can assign any non-forbidden scope. This reflects the current paw-enterprise deployment where workspace admins implicitly hold workspace-wide access. The comment acknowledges this is a temporary state: "until the scope-per-user model lands."

## `_scope_covers()`

The private helper implements the containment check:
- Exact match: `org:sales` covers `org:sales`
- Global wildcard: `*` covers everything (but `*` itself is forbidden to assign, so this path only applies in the admin check)
- Namespaced wildcard: `org:sales:*` covers `org:sales` and `org:sales:*`

## Known Gaps

- `admin_can_assign_scopes()` is defined but not yet called from the scope assignment endpoint — the PR comment notes the scope-per-admin model is tracked separately.
- There is no scope inheritance resolver: if an admin holds `org:*`, the system doesn't automatically infer they can assign `org:sales`.