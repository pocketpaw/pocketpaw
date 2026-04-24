---
{
  "title": "Cloud Files Permissions: ABAC Filtering and Capability Derivation Tests",
  "summary": "This module validates the layered permissions model in PocketPaw's cloud files system, covering ABAC tag filtering, RBAC-and-mount-writable capability intersection, and the `PermissionsEvaluator` composite. These tests ensure that no combination of user attributes, mount configuration, or entry tags can produce a capability set broader than what all three permission layers jointly allow.",
  "concepts": [
    "ABAC",
    "RBAC",
    "derive_capabilities",
    "PermissionsEvaluator",
    "apply_abac",
    "Permission",
    "mount writable",
    "capability intersection",
    "tag filtering",
    "access control"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Access Control",
    "Permissions",
    "test"
  ],
  "source_docs": [
    "17ba253178dff7e8"
  ],
  "backlinks": null,
  "word_count": 497,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_permissions.py` tests three interconnected functions from `ee.cloud.files.permissions`:

- **`apply_abac(entries, ctx, rules)`**: filters entries by ABAC tag rules, removing entries the caller is not cleared to see.
- **`derive_capabilities(entry, rbac, mount_writable, abac_allowed)`**: produces the final set of allowed operations for a single entry by intersecting RBAC permissions, mount write flag, and ABAC verdict.
- **`PermissionsEvaluator`**: a composite that applies ABAC filtering and capability annotation in a single pass.

## The Three-Layer Permission Model

PocketPaw's files permission system has three independent gates that all must pass:

1. **ABAC**: tag-based. If an entry carries a tag like `confidential`, the caller must satisfy the configured attribute requirements (e.g., `role=admin`). If not, the entry is entirely hidden.
2. **RBAC**: a `Permission(read, write, manage)` triple derived from the user's role in the workspace. Write operations require `write=True`; destructive operations like delete require `manage=True`.
3. **Mount writability**: even if RBAC grants write, a read-only mount configuration vetoes any mutation capability.

The `derive_capabilities` function takes the intersection of all three, producing a conservative final capability list.

## Test Breakdown

### `test_apply_abac_passes_untagged`

Two entries -- one untagged, one tagged `confidential` -- are filtered with a rule requiring `role=admin`. The context carries `role=member`. Only the untagged entry survives.

```python
out = apply_abac(entries, ctx=_ctx(role="member"), rules=rs)
assert [e.id for e in out] == ["uploads:x"]
```

This test exists because ABAC filtering is a visibility boundary, not just a capability boundary. A bug that returns both entries regardless of tag would leak confidential files silently.

### `test_apply_abac_allows_when_attr_matches`

Confirms the positive path: when `role=admin`, the confidential entry is included. Without this, a bug that always strips tagged entries would pass the first test but fail here.

### `test_derive_capabilities_intersects_rbac_and_mount_writable`

An entry carries `read`, `download`, `rename`, and `delete` capabilities from the provider. RBAC grants `read=True, write=False, manage=False`. The mount is also not writable. Expected result: only `read` and `download` survive.

```python
caps = derive_capabilities(entry=e, rbac=rbac, mount_writable=False, abac_allowed=True)
assert set(caps) == {"read", "download"}
```

This prevents privilege escalation through misconfigured RBAC -- even if a provider advertises `rename` as a capability, a read-only RBAC role strips it.

### `test_derive_capabilities_strips_all_when_abac_denies`

When `abac_allowed=False`, all capabilities are stripped to an empty list, regardless of RBAC or mount configuration. ABAC denial is total.

### `test_derive_capabilities_requires_manage_for_delete`

An entry has `delete` and `rename`. RBAC grants `write=True` but `manage=False`. The test asserts that `delete` is removed while `rename` remains. This encodes a specific business rule: deletion is a destructive, irreversible operation reserved for users with the `manage` permission, even if they have write access.

### `test_evaluator_filters_and_annotates`

The `PermissionsEvaluator` wraps both ABAC filtering and capability derivation. The test confirms that a `pii`-tagged entry is hidden from a caller with `clearance=low` -- the evaluator applies the full pipeline in one call, as the API layer would.

## Known Gaps

No tests cover what happens when `AbacRuleSet` contains conflicting rules for the same tag (e.g., two rules with different `require` conditions). The behavior in that case -- whether rules are ANDed, ORed, or first-match wins -- is not specified or tested here.
