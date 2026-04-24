---
{
  "title": "ABAC Config Tests: Rule Loading and Allow/Deny Logic",
  "summary": "This module tests the Attribute-Based Access Control (ABAC) configuration layer for the files subsystem, covering YAML rule loading, the allow logic for tagged vs. untagged entries, attribute matching, and the deny-override behavior when a file carries multiple tags.",
  "concepts": [
    "ABAC",
    "AbacRule",
    "AbacRuleSet",
    "load_rules",
    "YAML config",
    "tag-based access",
    "attribute matching",
    "deny override",
    "multi-tag",
    "RBAC layering",
    "data classification",
    "security policy"
  ],
  "categories": [
    "testing",
    "security",
    "ABAC",
    "files",
    "test"
  ],
  "source_docs": [
    "tests/cloud/files/test_abac_config.py"
  ],
  "backlinks": null,
  "word_count": 526,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_abac_config.py` covers `AbacRule`, `AbacRuleSet`, and `load_rules` from `ee.cloud.files.abac_config`. ABAC (Attribute-Based Access Control) is the files subsystem's policy layer that sits on top of the baseline RBAC provided by each storage provider. While RBAC checks whether a user can access a file at all, ABAC applies tag-based policies that further restrict access based on user attributes (role, clearance level, department, etc.).

## load_rules: YAML Deserialization

### Empty Rules
`test_load_rules_empty` writes a minimal YAML file with `rules: []` and asserts that `load_rules` returns an `AbacRuleSet` with an empty rules list. This tests the base case — a config file with no rules permits everything by default.

### Rule Shape Parsing
`test_load_rules_parses_shape` writes a YAML file with one rule and asserts that the parsed `AbacRule` has the correct `tag` and `require` fields:

```yaml
rules:
  - tag: confidential
    require:
      role: [admin, owner]
```

The parsed rule must have `r.tag == "confidential"` and `r.require == {"role": ["admin", "owner"]}`. This pins the YAML schema — if the field names change, this test fails before any runtime failure.

## AbacRuleSet.allows: Access Decision Logic

### Untagged Entry Always Allowed
`test_ruleset_allows_entry_when_untagged` asserts that an entry with no tags passes ABAC regardless of the user's attributes. ABAC rules only activate when an entry carries a matching tag. This is the correct default: a file without sensitive tags should not be blocked by policies meant for confidential content.

### Attribute Match → Allow
`test_ruleset_allows_when_attribute_matches` asserts that an entry tagged `"confidential"` is accessible when the user's context includes `role="admin"`. The `require` field lists the allowed values; if the user's attribute value is in that list, access is granted.

### Attribute Mismatch → Deny
`test_ruleset_denies_when_attribute_mismatches` asserts that the same entry is denied when the user has `role="member"` (not in the allowed list). This is the core enforcement test — without it, the allow/deny logic could be inverted.

### Deny Overrides Multiple Tags
```python
def test_ruleset_deny_overrides_multiple_tags():
    rs = AbacRuleSet(rules=[
        AbacRule(tag="confidential", require={"role": ["admin"]}),
        AbacRule(tag="pii", require={"clearance": ["high"]}),
    ])
    assert not rs.allows(
        tags=["confidential", "pii"],
        attributes={"role": "admin", "clearance": "low"}
    )
```

This is the most important security test in the module. When a file carries multiple tags, all matching rules must pass — a single failing rule denies access regardless of other matching rules. In this case, the user satisfies the `confidential` rule (admin role) but fails the `pii` rule (clearance is "low" not "high"). The result must be deny.

Without this test, an implementation that uses `any()` instead of `all()` across matching rules would incorrectly grant access because the admin role rule was satisfied.

## Why ABAC Sits Above RBAC

The provider's `baseline_rbac` determines whether the user can access the file at the provider level (e.g., "is this user the owner or an admin?"). ABAC then applies workspace-level policy on top: even if a user is an admin, a `pii` tag might require an additional `clearance` attribute. This two-layer design keeps provider logic focused on ownership semantics while ABAC handles data classification policy.

## Known Gaps

No TODOs or FIXMEs are present. Tests do not cover wildcard attribute values or operator semantics (e.g., `role: ["admin", "*"]` to match any role). These would be relevant if the ABAC rule language is extended.
