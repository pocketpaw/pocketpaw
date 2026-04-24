---
{
  "title": "ACTIONS Registry: Canonical RBAC Action-to-Role Matrix",
  "summary": "The `actions.py` module is the single source of truth for every guarded operation in PocketPaw, mapping action strings (in `\"resource.action\"` dotted form) to a minimum required role and a stable machine-readable denial code. Unknown actions fail loudly by design, preventing silent authorization bypasses when new routes are added without a corresponding ACTIONS entry.",
  "concepts": [
    "ACTIONS registry",
    "ActionRule",
    "GroupRole",
    "WorkspaceRole",
    "deny code",
    "fail-loud design",
    "fleet.install auth guard",
    "authorization matrix",
    "check_action",
    "role hierarchy"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "authorization",
    "RBAC"
  ],
  "source_docs": [
    "de67fd205d5a3968"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`actions.py` (`src/pocketpaw/ee/guards/actions.py`) defines the complete authorization matrix for PocketPaw's EE layer. Every guarded operation — from workspace management to pocket access to fleet installation — must have an entry in the `ACTIONS` dict before any route can call `check_workspace_action()` or `require_policy()` against it.

## GroupRole: Group-Scoped Authorization

Alongside the workspace-level `WorkspaceRole`, `actions.py` defines `GroupRole` for channel/group-scoped authorization:

```python
class GroupRole(StrEnum):
    VIEW = "view"
    MEMBER = "edit"
    ADMIN = "admin"
    OWNER = "owner"
```

Note that `MEMBER` maps to the string value `"edit"` — this reflects how group roles are stored in the database (`Group.member_roles`), where "edit" is the posting-capable role and "view" is read-only. The enum alias prevents the rest of the codebase from having to know this storage-level detail.

## ActionRule

```python
@dataclass(frozen=True, slots=True)
class ActionRule:
    minimum: RoleType
    deny_code: str
```

`ActionRule` pairs a minimum role with a **stable deny code**. The deny code is the string the frontend keys off of to show the correct error message (e.g., `"group.view_only"` vs. `"workspace.insufficient_role"`). By making deny codes part of the rule definition rather than generated at enforcement time, frontend and backend stay in sync: a frontend unit test can assert that `group.post` denial emits `"group.view_only"` without mocking the entire authorization stack.

## The ACTIONS Dict

The `ACTIONS` dict covers workspace, group, message, pocket, agent, session, knowledge base, invite, billing, and fleet operations. It uses the `"resource.action"` dotted naming convention so that action prefixes can be mapped to plan features by the ABAC evaluator.

A notable recent addition:

```python
"fleet.install": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
```

This was added in `fix/fleet-install-auth-guard` (2026-04-19) to close a P0 auth bypass where the fleet router had no role check — any authenticated workspace member could previously trigger fleet installs.

## Fail-Loud for Unknown Actions

```python
def get_rule(action: str) -> ActionRule:
    try:
        return ACTIONS[action]
    except KeyError:
        raise KeyError(
            f"Unknown action {action!r}. Register it in ACTIONS before guarding a route."
        ) from None
```

This is an intentional fail-loud design. If a developer adds a new route and calls `check_workspace_action(..., "pocket.archive")` without adding `"pocket.archive"` to `ACTIONS`, the `KeyError` will surface in tests and during development — not silently pass as allowed. The alternative (returning a default-allow rule) would be catastrophic for security.

## check_action Type Safety

```python
def check_action(action: str, actor_level: RoleType) -> None:
    rule = get_rule(action)
    if type(actor_level) is not type(rule.minimum):
        raise TypeError(...)
```

The type guard prevents mixing `WorkspaceRole` with `GroupRole` comparisons, which would produce nonsensical level comparisons since both enums have a `.level` property but the numbers are not comparable across namespaces.

## Known Gaps

- Tests are mentioned as iterating `ACTIONS` to guarantee coverage, but this test is in a different file. If `ACTIONS` grows without a corresponding test assertion, coverage could silently drop.
- Some actions have `WorkspaceRole.MEMBER` as minimum, meaning all authenticated workspace members can perform them. There is no "authenticated but not a member" tier, so cross-workspace action attempts fall through to the workspace membership check in `deps.py`.