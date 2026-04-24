---
{
  "title": "Pocket Layout Export and User-Defined Template Registry",
  "summary": "This module provides two distinct but related capabilities: a pure YAML serialisation of a pocket's ripple spec for export or sharing, and an in-process workspace-scoped store for user-defined pocket templates. Both capabilities back new REST endpoints added in Cluster B Sub-PR #3 to close a gap in the UI testing guide around widget layout save and share.",
  "concepts": [
    "YAML export",
    "PocketLayout",
    "ripple spec",
    "user template",
    "UserTemplateStore",
    "UserPocketTemplate",
    "workspace scoping",
    "FastAPI dependency injection",
    "in-process store",
    "safe_dump",
    "parse_layout_yaml",
    "template registry",
    "deterministic serialisation"
  ],
  "categories": [
    "pockets",
    "layout",
    "templates",
    "YAML",
    "EE cloud"
  ],
  "source_docs": [
    "5fbb922d28674259"
  ],
  "backlinks": null,
  "word_count": 556,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/pockets/layouts.py` was introduced to close a specific gap (UI-TESTING-GUIDE §11, gap B5) where operators had no way to export a pocket's layout or save their own templates for reuse. It covers two distinct surfaces that share a common data type: the layout YAML export (pure, stateless) and the user template registry (mutable, workspace-scoped).

## YAML Export: Pure and Deterministic

The `export_layout_yaml` function serialises a pocket's layout into a `PocketLayout` YAML document. The design is intentionally stateless — given the same pocket data it will always produce byte-identical output because `sort_keys=True` is passed to PyYAML's `safe_dump`. That determinism is load-bearing: the test suite performs a round-trip export-then-import and diffs the result, so any non-determinism would cause spurious test failures.

The function accepts both `ripple_spec` (the modern canonical spec stored in the `rippleSpec` field) and a legacy `widgets` list. When both are present, `ripple_spec` takes precedence — this reflects the evolution of the pocket document model where the flat widgets array is now considered a legacy mirror.

```python
body: dict[str, Any] = {
    "apiVersion": "pocketpaw.io/v1",
    "kind": "PocketLayout",
    "spec": ripple_spec or {"widgets": list(widgets or [])},
}
return yaml.safe_dump(body, sort_keys=True, default_flow_style=False)
```

YAML is lazy-imported (`import yaml` inside the function body) to match the pattern established in `ee/fleet/installer.py`. This avoids adding PyYAML as a hard top-level import and keeps the module cheap to import in contexts where YAML is never needed.

`parse_layout_yaml` is the mirror-image function: it safely loads a user-supplied YAML string and validates that it conforms to the expected `kind: PocketLayout` / `spec` structure. Critically, the error messages produced by its `ValueError` raises are safe to return to callers — they do not leak filesystem paths or Python internals. This matters because the router surfaces parse errors directly to the frontend as 400 responses that appear inline in the save-as-template dialog.

## In-Process User Template Store

The `UserTemplateStore` class is a plain in-memory dict keyed by `(workspace_id, template_id)` tuples. It exposes `save`, `list_for_workspace`, `get`, and `reset`. The deliberate choice of in-process storage rather than a database is an MVP tradeoff: the team needed a working read/write surface quickly for the demo-readiness push, and the REST contract was designed to be stable enough that a MongoDB-backed version can slot in later without changing any routes or response shapes.

```python
class UserTemplateStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], UserPocketTemplate] = {}

    def save(self, template: UserPocketTemplate) -> UserPocketTemplate:
        self._rows[(template.workspace_id, template.id)] = template
        return template
```

The module-level singleton `_store` is accessed via `get_user_template_store()`, a FastAPI dependency. This indirection is specifically designed to allow tests to swap the store instance via dependency override — without it, tests would share state across test functions and cause ordering-dependent failures.

`reset_user_template_store()` is a test-only convenience function that clears the singleton. It is exported so fixtures can call it in teardown without importing the private `_store` directly.

## Known Gaps

- **No persistence**: The template store is explicitly in-process only. Templates do not survive a process restart. The module comment calls out Wave 4 / Cluster G as the intended migration point to MongoDB-backed persistence.
- **No pagination**: `list_for_workspace` returns all templates for a workspace with no limit or cursor. For workspaces that accumulate many templates this will become a problem.
- **No delete endpoint**: There is no way to remove a saved template; only creation and listing are supported in this iteration.