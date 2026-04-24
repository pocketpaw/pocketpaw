---
{
  "title": "Agent Scope Rules: Normalization and Assignment Authorization Tests",
  "summary": "This module tests `ee.cloud.agents.scope_rules`, covering the `normalise_and_validate_scopes` function (grammar enforcement, lowercasing, deduplication, forbidden-wildcard rejection) and the `admin_can_assign_scopes` function (containment check that prevents scope-narrowed admins from assigning scopes outside their own grant).",
  "concepts": [
    "scope_rules",
    "normalise_and_validate_scopes",
    "admin_can_assign_scopes",
    "ScopeValidationError",
    "FORBIDDEN_SCOPES",
    "scope grammar",
    "privilege escalation",
    "wildcard",
    "authorization",
    "containment check",
    "agent scopes"
  ],
  "categories": [
    "agents",
    "authorization",
    "testing",
    "scope management",
    "test"
  ],
  "source_docs": [
    "28f3aebd764c4c4f"
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

The `tests/cloud/test_agent_scope_rules.py` module was created for the `feat/cluster-d-agent-scope-picker` feature. Scope strings are the authorization primitive for agent data access — getting the validation rules wrong can grant agents too much or too little access, or enable privilege escalation attacks.

The module is divided into two test classes, each covering a distinct function in `ee.cloud.agents.scope_rules`.

## TestNormaliseAndValidateScopes

This class exercises `normalise_and_validate_scopes`, which takes a list of raw scope strings from user input and returns a clean, deduplicated list — or raises `ScopeValidationError` for invalid inputs.

### Normalization
- Whitespace is stripped and all characters are lowercased: `"  Org:Sales:Leads  "` becomes `"org:sales:leads"`.
- Deduplication preserves the first occurrence's order: `["org:sales:*", "org:sales:*", "org:marketing"]` becomes `["org:sales:*", "org:marketing"]`.

### Grammar Rules
The scope grammar allows hierarchical colon-delimited segments with an optional trailing wildcard:
- **Namespaced wildcard accepted**: `"org:sales:*"` is valid — the wildcard only covers the trailing segment.
- **Universal wildcard rejected**: `"*"` alone is in `FORBIDDEN_SCOPES` because it grants all access across all namespaces.
- **Mid-segment wildcard rejected**: `"org:*:leads"` raises `ScopeValidationError` with message containing `"mid-segment wildcard"` — this pattern has no valid semantic meaning in PocketPaw's scope model.
- **Empty segment rejected**: `"org::leads"` is invalid because double colons produce an empty segment.
- **Leading colon rejected**: `":org:leads"` is invalid.
- **Empty string rejected**: An empty string is not a valid scope.
- **Non-string rejected**: Passing a non-string element raises `ScopeValidationError`.

```python
def test_mid_segment_wildcard_rejected(self):
    with pytest.raises(ScopeValidationError) as exc:
        normalise_and_validate_scopes(["org:*:leads"])
    assert "mid-segment wildcard" in str(exc.value)
```

## TestAdminCanAssignScopes

This class tests `admin_can_assign_scopes(admin_scopes, requested_scopes)`, which enforces that a scope-narrowed admin cannot assign scopes they don't themselves hold. This is a containment check: the requested scopes must be a subset of the admin's own grant.

### Key Scenarios
- **Empty admin scopes**: An admin with no scope restrictions can assign anything that is not forbidden. This represents a super-admin.
- **Exact match**: `["org:sales"]` admin can assign `["org:sales"]`.
- **Glob coverage**: `["org:sales:*"]` admin covers `["org:sales:leads"]` — a glob admin covers all descendants.
- **Escape prevention**: An admin with `["org:sales"]` cannot assign `["org:marketing"]` or `["org:sales:*"]` (wider than their own grant).
- **Union of admin scopes**: Multi-scope admins `["org:sales", "org:marketing"]` cover the union — a requested scope matching either is allowed.
- **Wildcard admin covers all**: `["*"]` on the admin side (super-admin) covers all requested scopes.

This prevents privilege escalation via the scope assignment endpoint, where a delegated admin could otherwise grant access beyond their own authorization boundary.

## Known Gaps

No TODO or FIXME markers. The tests do not cover deeply nested scopes beyond two or three levels. Cross-namespace scope interactions (e.g., an `org:sales` admin assigning `vendor:acme:*`) are not explicitly tested. The `FORBIDDEN_SCOPES` set content is only partially verified (universal wildcard confirmed, but other entries are not tested).