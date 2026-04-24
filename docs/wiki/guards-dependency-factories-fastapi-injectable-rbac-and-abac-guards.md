---
{
  "title": "Guards Dependency Factories: FastAPI-Injectable RBAC and ABAC Guards",
  "summary": "The `deps.py` module generates FastAPI dependency callables that enforce authorization on route handlers, pulling workspace membership and role from `request.state` (populated by upstream middleware) and raising `HTTPException(403)` on any denial. It also provides non-FastAPI helper functions for cloud routes that authenticate via fastapi-users and carry full user models rather than request state.",
  "concepts": [
    "FastAPI dependency injection",
    "require_role",
    "require_policy",
    "require_plan_feature",
    "ABAC evaluation",
    "workspace membership",
    "agent ceiling",
    "resolve_workspace_role",
    "check_workspace_action",
    "Forbidden exception"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "authorization",
    "FastAPI"
  ],
  "source_docs": [
    "bd48db72578d5f56"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`deps.py` (`src/pocketpaw/ee/guards/deps.py`) is the integration layer between PocketPaw's authorization logic and the FastAPI dependency injection system. It turns the RBAC/ABAC primitives into reusable `Depends()` callables that route handlers can declare without writing any authorization code inline.

## Dependency Factory Pattern

Each public function in this module is a **factory** — it takes configuration parameters and returns an async callable (`_GuardDep`) that FastAPI injects at request time:

```python
def require_role(*roles: WorkspaceRole | str) -> _GuardDep:
    resolved = [WorkspaceRole.from_str(r) if isinstance(r, str) else r for r in roles]
    minimum = min(resolved, key=lambda r: r.level)

    async def _guard(request: Request) -> None:
        _get_user_context(request)  # enforce authentication first
        ws_id = _get_workspace_id(request)
        membership = getattr(request.state, "workspace_membership", None)
        if membership is None or membership.get("workspace_id") != ws_id:
            raise HTTPException(status_code=403, detail="Not a member of this workspace")
        try:
            check_workspace_role(membership.get("role", ""), minimum=minimum)
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc

    return _guard
```

The factory resolves roles at **definition time** (when the route module is imported), not at request time. This catches configuration errors early — if `require_role("superadmin")` is called but `WorkspaceRole` has no `superadmin` value, the `ValueError` fires at startup.

## Workspace ID Extraction

`_get_workspace_id()` checks both the `X-Workspace-Id` header and the `workspace_id` query parameter. This dual-source extraction exists because different client types prefer different transport mechanisms: browser-based dashboards typically use headers, while webhook handlers or CLI tools may use query params.

## require_policy: Full ABAC Evaluation

`require_policy(action)` is the highest-level guard, running the complete ABAC pipeline including plan gates, role checks, and agent ceilings:

```python
def require_policy(action: str) -> _GuardDep:
    async def _guard(request: Request) -> None:
        # ... extract context from request.state ...
        policy_ctx = PolicyContext(
            user_id=..., workspace_id=..., role=..., action=action,
            agent_id=agent_id, agent_creator_role=agent_creator_role, ...
        )
        result = evaluate_policy(policy_ctx)
        if not result.allowed:
            raise HTTPException(status_code=403, detail=result.code)
    return _guard
```

Agent context (creator role) is populated from `request.state.agent_context` — a dict injected by agent execution middleware when an agent is making the request on behalf of a user. This enables the agent permission ceiling check in the ABAC evaluator.

## User-Model-Backed Helpers

For cloud routes that use fastapi-users, the `request.state`-based approach doesn't apply — the full user document is available directly. Two helper functions handle this:

- `resolve_workspace_role(user, workspace_id)`: walks `user.workspaces` to find the role for a given workspace, raising `Forbidden` with `"workspace.not_member"` if not found.
- `check_workspace_action(user, workspace_id, action)`: resolves the role then runs `check_action()`, automatically calling `log_denial()` on failure. This is the correct function to call in cloud route handlers — it handles role resolution, action enforcement, and audit logging in one call.

## Known Gaps

- `require_pocket_access` extracts the pocket membership from `request.state.pocket_membership`, which must be set by a pocket-resolution middleware before the guard runs. If the route is mounted without that middleware, the guard will always 403.
- There is no explicit check for the active workspace plan in `require_role` — a user with the right role on a team-plan workspace can still pass `require_role("admin")` even if the action they're about to perform is an enterprise feature. The plan check must be layered via `require_plan_feature` or `require_policy`.