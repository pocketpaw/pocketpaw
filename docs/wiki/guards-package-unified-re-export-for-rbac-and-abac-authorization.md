---
{
  "title": "Guards Package: Unified Re-Export for RBAC and ABAC Authorization",
  "summary": "The `guards` package `__init__.py` is a single-point re-export that collects all authorization primitives — roles, access levels, policy evaluation, action rules, FastAPI dependency factories, and audit helpers — under one importable namespace. This prevents callers from needing to know which sub-module owns each symbol, and makes future refactoring transparent to the rest of the codebase.",
  "concepts": [
    "re-export pattern",
    "RBAC",
    "ABAC",
    "WorkspaceRole",
    "PocketAccess",
    "GroupRole",
    "PolicyContext",
    "FastAPI dependencies",
    "Forbidden exception",
    "authorization layer"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "authorization",
    "package structure"
  ],
  "source_docs": [
    "c33d7fb15cb09ae1"
  ],
  "backlinks": null,
  "word_count": 398,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `pocketpaw.ee.guards` package (`src/pocketpaw/ee/guards/__init__.py`) acts as the public API surface for PocketPaw's authorization layer. Rather than exposing six separate modules (`abac`, `actions`, `audit`, `deps`, `policy`, `rbac`), it re-exports every public symbol through one namespace.

## Why a Re-Export Layer?

Without this file, every caller needing role checks and audit logging would write:

```python
from pocketpaw.ee.guards.rbac import WorkspaceRole, Forbidden
from pocketpaw.ee.guards.deps import require_role
from pocketpaw.ee.guards.audit import log_denial
```

With the re-export, they write:

```python
from pocketpaw.ee.guards import WorkspaceRole, Forbidden, require_role, log_denial
```

This matters for a module that is imported in many route files across the EE codebase. If a symbol moves between sub-modules (e.g., `evaluate_policy` is pulled from `abac` into a new `engine` module), the only change needed is in this `__init__.py` — all callers continue to work.

## What Is Exported

The `__all__` list spans the full guard surface:

- **Core role enums**: `WorkspaceRole`, `PocketAccess`, `GroupRole` — the numeric-level enums that drive all authorization comparisons
- **Policy types**: `PolicyContext`, `PolicyResult` — frozen dataclasses that carry authorization input and output
- **ABAC engine**: `evaluate_policy`, `ACTION_ROLES`, `PLAN_FEATURES`, `ROLE_TOOL_LIMITS` — the declarative policy tables and their evaluator
- **Action registry**: `ACTIONS`, `ActionRule`, `get_rule`, `check_action`, `check_group_role` — the canonical action-to-minimum-role matrix
- **FastAPI deps**: `require_role`, `require_pocket_access`, `require_plan_feature`, `require_policy`, `resolve_workspace_role`, `check_workspace_action`, `check_group_action`, `make_require_action`, `resolve_group_role` — dependency factories for use in `Depends()`
- **Audit helpers**: `log_denial`, `log_privileged_action` — structured event emitters for the security audit trail
- **Exception**: `Forbidden` — the machine-coded authorization failure exception

## Architectural Significance

This approach makes the guards layer **dependency-injectable from a single import**. FastAPI route files can declare `dependencies=[Depends(require_role("admin"))]` with one import, keeping route files focused on business logic rather than importing from four different sub-modules.

The package also makes testing more ergonomic: a test that needs to override authorization can mock `pocketpaw.ee.guards.require_role` at one location rather than patching individual sub-modules.

## Known Gaps

- The `__all__` list is hand-maintained. If a developer adds a symbol to a sub-module but forgets to add it to `__init__.py` and `__all__`, it becomes effectively private — which may or may not be the intent. A linting rule or test that asserts `__all__` completeness would prevent this drift.
- There is no lazy import here: importing `pocketpaw.ee.guards` eagerly imports all six sub-modules, including `deps.py` which imports FastAPI. In contexts where FastAPI is not installed (e.g., CLI-only usage), this would fail at import time even if no guard is invoked.