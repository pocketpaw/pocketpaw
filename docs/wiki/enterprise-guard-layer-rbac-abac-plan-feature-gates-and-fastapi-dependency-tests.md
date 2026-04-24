---
{
  "title": "Enterprise Guard Layer: RBAC, ABAC, Plan Feature Gates, and FastAPI Dependency Tests",
  "summary": "Comprehensive tests for PocketPaw's enterprise authorization layer, covering role-based access control (RBAC) enums and guard functions, attribute-based access control (ABAC) policy evaluation, plan feature tables, action-to-role mapping, tool whitelists, and three FastAPI dependency guards (`require_role`, `require_plan_feature`, `require_policy`).",
  "concepts": [
    "RBAC",
    "ABAC",
    "WorkspaceRole",
    "PocketAccess",
    "PolicyContext",
    "evaluate_policy",
    "plan feature gate",
    "agent ceiling",
    "tool whitelist",
    "require_policy dependency"
  ],
  "categories": [
    "security",
    "authorization",
    "enterprise",
    "testing",
    "test"
  ],
  "source_docs": [
    "627d970899ddef15"
  ],
  "backlinks": null,
  "word_count": 602,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why a Multi-Layer Guard System

PocketPaw's authorization model has three independent dimensions that must all be satisfied:

1. **Plan tier** — does the workspace's subscription include this feature?
2. **Role** — does the user have sufficient role level (member < admin < owner) for this action?
3. **Agent ceiling** — if the caller is an agent (not a human), is its granted permission level sufficient?

A single role check is insufficient because a workspace admin on the community plan must not access enterprise features. A plan check alone is insufficient because a workspace member must not access billing settings even on enterprise. The `require_policy` dependency enforces all three simultaneously.

## TestWorkspaceRole and TestPocketAccess

Both enums use integer level fields for comparison (`member=1`, `admin=2`, `owner=3`; `view=1`, `comment=2`, `edit=3`, `owner=4`). Tests verify exact level values because guards use `>=` comparisons — if a level value changes, gates silently shift.

`test_from_str_invalid_raises_valueerror` verifies that `WorkspaceRole.from_str("superadmin")` raises `ValueError` rather than defaulting to a valid role. Defaulting would be a privilege escalation vector.

## TestCheckWorkspaceRole and TestCheckPocketAccess

The `check_workspace_role(user_role, minimum)` and `check_pocket_access(user_access, minimum)` guard functions take roles/access levels and return a boolean. They are pure functions without side effects — no HTTP, no database.

Key cases:

- Owner passes admin check, admin passes admin check, member fails admin check.
- Admin fails owner check (owner-only gates are strict).
- Raw string inputs are accepted (callers need not construct enum objects).
- Invalid role strings raise `ValueError` immediately.

## TestEvaluatePolicy: Full ABAC

The `evaluate_policy(context, action)` function takes a `PolicyContext` (plan, role, agent ceiling, tool being called) and an action string and returns a `PolicyResult` with an `allowed` boolean and optional denial `code` and `detail`.

```python
result = evaluate_policy(
    PolicyContext(plan="team", role="member"),
    action="pocket.create"
)
assert result.allowed
```

Key scenarios tested:

- **Plan gate** — `team` plan allows `pockets` feature, blocks `automations` feature. `enterprise` plan allows all features.
- **Role gate** — `pocket.create` requires `member` level; `billing.manage` requires `owner`.
- **Agent ceiling** — if the context includes an `agent_ceiling` of `"member"`, an agent cannot perform `admin`-level actions even if the human user is an admin. This prevents agent privilege escalation.
- **Unknown action default** — actions not in the `ACTION_ROLES` table default to `member` allowed, rather than denying or erroring. This is a permissive default that keeps unrecognized actions from breaking unexpectedly.
- **Tool whitelist** — `member` role cannot call `shell_execute` but can call `web_search`. `admin` and `owner` have no tool restrictions.

## TestPlanFeatureTable

The `PLAN_FEATURES` constant maps plan names to sets of feature strings. Three tests verify the plan hierarchy is strictly additive:

- `business` is a strict superset of `team`.
- `enterprise` is a strict superset of `business`.
- `enterprise` includes `audit_log` and `sso` which lower tiers do not.

This prevents accidental feature removal when adding new features to higher tiers.

## TestRequireRoleDep, TestRequirePlanFeatureDep, TestRequirePolicyDep

Three FastAPI dependency tests use mini-apps with middleware that injects fake user context (role, plan, workspace ID) into the request state. Each dependency is tested for:

- **Happy path** — correct role/plan allows the request through.
- **Denial path** — insufficient role/plan returns 403.
- **Missing context** — no user in request state returns 401 (unauthenticated, not unauthorized).
- **Missing workspace ID** — 403 when workspace context is absent.

The agent ceiling tests verify that the `require_policy` dependency correctly blocks an agent from escalating past its ceiling even when the human user has higher privileges.

## Known Gaps

The `unknown action defaults to member allowed` behavior is intentional but potentially dangerous if internal admin actions are accidentally omitted from `ACTION_ROLES`. A periodic audit of the table against the router's action strings would catch gaps.