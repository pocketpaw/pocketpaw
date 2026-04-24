---
{
  "title": "ABAC Policy Engine: Plan Gates, Role Checks, and Agent Ceilings",
  "summary": "The `abac.py` module implements PocketPaw's attribute-based access control (ABAC) by combining three declarative tables — plan feature gates, action-to-role mappings, and per-role tool limits — into a single `evaluate_policy` function. It is designed to run after RBAC role resolution, adding plan-tier and agent-ceiling checks that role membership alone cannot express.",
  "concepts": [
    "ABAC",
    "plan feature gates",
    "agent ceiling",
    "tool whitelist",
    "PolicyContext",
    "PolicyResult",
    "evaluate_policy",
    "privilege escalation prevention",
    "WorkspaceRole",
    "action-role mapping"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "authorization",
    "access control"
  ],
  "source_docs": [
    "1996a6e3f667c93f"
  ],
  "backlinks": null,
  "word_count": 493,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`abac.py` (`src/pocketpaw/ee/guards/abac.py`) sits above the RBAC primitives in the authorization stack. Where RBAC answers "does this user have the right role?", ABAC answers "does the user's plan allow this feature?" and "is this agent allowed to act with this level of permission?"

## Plan Feature Gates

```python
PLAN_FEATURES: dict[str, set[str]] = {
    "team": {"pockets", "sessions", "agents", "memory"},
    "business": {"pockets", "sessions", "agents", "memory", "automations", "fabric", "knowledge_base"},
    "enterprise": {"pockets", "sessions", "agents", "memory", "automations", "fabric",
                   "instinct", "knowledge_base", "audit", "sso", "custom_roles"},
}
```

This table enforces commercial plan boundaries at the authorization layer rather than at the feature UI layer. By putting the gate in the policy evaluator, even direct API calls to enterprise endpoints are blocked for team-tier workspaces — there is no way to accidentally expose a feature by forgetting to hide a button in the UI.

## Action-to-Role Mapping

`ACTION_ROLES` maps action strings like `"workspace.delete"` to a minimum `WorkspaceRole`. This is distinct from the `ACTIONS` dict in `actions.py`: `ACTION_ROLES` is used by the ABAC evaluator's integrated role check, while `ACTIONS` is the canonical registry used by FastAPI dependency factories. Both serve the same logical purpose but operate in different call paths.

## Agent Permission Ceiling

One of the more sophisticated checks is the agent ceiling:

```python
if ctx.agent_id is not None and ctx.agent_creator_role is not None:
    if ctx.role.level > ctx.agent_creator_role.level:
        return PolicyResult(
            allowed=False,
            code="agent.ceiling_exceeded",
            detail=f"Agent {ctx.agent_id} was created by {ctx.agent_creator_role.value}, "
                   f"cannot act as {ctx.role.value}",
        )
```

This prevents privilege escalation via agents. Without this check, a MEMBER who creates an agent could configure that agent to perform ADMIN-level actions on their behalf. The ceiling ensures that an agent can never exceed the permissions of its creator at the time of creation.

## Tool Whitelist

```python
ROLE_TOOL_LIMITS: dict[WorkspaceRole, set[str] | None] = {
    WorkspaceRole.MEMBER: {"web_search", "research", "memory", "soul_recall", "soul_remember"},
    WorkspaceRole.ADMIN: None,
    WorkspaceRole.OWNER: None,
}
```

MEMBERS are limited to a safe subset of tools; `None` for ADMIN and OWNER means no restriction. When `ctx.action` starts with `"tool."`, the evaluator extracts the tool name and checks it against the caller's role's allowed set. This prevents a MEMBER from calling destructive tools like `shell_exec` through a prompt injection.

## Evaluation Order

`evaluate_policy` runs checks in order: plan gate, role minimum, agent ceiling, tool whitelist. It returns the **first denial** encountered — the fail-fast pattern prevents attackers from using one passing check to infer information about subsequent checks.

## Known Gaps

- `_feature_for_action` maps only five action prefixes to plan features. Actions like `"kb.write"` or `"session.read_any"` would not be gated by plan even if `knowledge_base` is an enterprise feature, unless the caller also uses `require_plan_feature` directly.
- There is no caching of `PLAN_FEATURES.get(ctx.plan, set())` — each policy evaluation re-does the dict lookup. For high-traffic deployments this is negligible, but it is worth noting.
- The agent ceiling only fires if both `agent_id` and `agent_creator_role` are present in the context. If the middleware that populates `agent_context` is skipped or misconfigured, the ceiling is silently not enforced.