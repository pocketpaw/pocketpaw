---
{
  "title": "Fabric Scope Policy Engine — Visibility Decisions for Ontology Objects",
  "summary": "This module provides the scope-based visibility engine for Fabric objects, implementing bidirectional containment matching so that a caller with scope `org:sales:*` can see objects tagged `org:sales:leads` and vice versa. It is a local copy of the logic from soul-protocol's `scopes_overlap` function, kept independent to avoid pulling the full engine into minimal runtime slices and to preserve the audit trail of which scope granted access.",
  "concepts": [
    "scope policy",
    "bidirectional containment",
    "DEFAULT_ALLOW_UNSCOPED",
    "PolicyDecision",
    "filter_visible",
    "visible",
    "decide",
    "matched_scope",
    "duck-typed entity",
    "audit trail",
    "scopes_overlap"
  ],
  "categories": [
    "fabric",
    "scope policy",
    "access control",
    "visibility filtering",
    "audit"
  ],
  "source_docs": [
    "01b13b101868df26"
  ],
  "backlinks": null,
  "word_count": 397,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Design Rationale

The module header explains two reasons why the policy engine is a local copy rather than a direct call to soul-protocol's `scopes_overlap`:

1. **Minimal dependency footprint**: The policy engine must be importable in test fixtures and CLI tools that do not want to initialize the full soul-protocol engine. A hard import of `soul_protocol.engine` would drag in journal storage, memory tiers, and other subsystems.
2. **Audit trail**: `decide()` returns the exact caller scope that granted access — a `PolicyDecision` dataclass with `matched_scope`. `scopes_overlap` returns a bool and discards this information. Downstream consumers use `matched_scope` for observability ("why did this query return this object?").

## Bidirectional Containment

The `_granted(entity_scope, allowed_scope)` function implements bidirectional containment: a match occurs when either scope is a prefix of the other (wildcard on either side). This means:

- `org:sales:leads` is visible to a caller with scope `org:sales:*` (caller is broader).
- `org:sales:*` is visible to a caller with scope `org:sales:leads` (entity is broader).

This matches soul-protocol's `scopes_overlap` semantics exactly, ensuring Fabric and paw-runtime's retrieval router produce identical results for the same caller and entity combination.

## `DEFAULT_ALLOW_UNSCOPED`

The module-level flag `DEFAULT_ALLOW_UNSCOPED = True` controls whether entities with an empty `scope` list are visible to everyone. The default is `True` (permissive) because most early Fabric objects predate scope tagging. Tenants that require explicit scope on all entities can flip this to `False` at process start; per-call overrides flow through the `allow_unscoped` keyword argument.

## Core Functions

- **`visible(entity, user_scopes)`** — single-entity check. Returns `True` when the caller should see this object. The `entity` argument is duck-typed: anything with a `scope` attribute or key works, making it compatible with `FabricObject`, plain dicts, and test stubs.
- **`filter_visible(entities, user_scopes)`** — batch filter. Returns `(kept_list, hidden_count)`. The hidden count feeds the projection's retrieval log so operators can see how much was filtered per call.
- **`decide(entity, user_scopes)`** — returns a full `PolicyDecision` with `allowed`, `entity_id`, `entity_scopes`, `matched_scope`, and `reason`. Used by audit and observability paths.

## Integration with the Projection

The projection calls `filter_visible` during `query()` to apply scope before computing `total`. This is what prevents the pagination leak that plagued PR #938: `total` in the result is always derived from the post-filter list, never from a pre-filter count.

## Known Gaps

No known gaps. The policy module is ported verbatim from #938's `ee/policy/engine.py` which carried its own test suite. The logic is considered stable.