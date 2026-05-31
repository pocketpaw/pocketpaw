# FastAPI dependency factories — RBAC/ABAC guard injection for route handlers.
# Created: 2026-04-10

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request

from pocketpaw_ee.guards.abac import evaluate_policy
from pocketpaw_ee.guards.actions import (
    ActionRule,
    GroupRole,
    check_action,
    check_group_role,
    get_rule,
)
from pocketpaw_ee.guards.audit import log_denial
from pocketpaw_ee.guards.policy import PolicyContext
from pocketpaw_ee.guards.rbac import (
    Forbidden,
    PocketAccess,
    WorkspaceRole,
    check_pocket_access,
    check_workspace_role,
)

logger = logging.getLogger(__name__)

# Type alias for FastAPI dependency callables
_GuardDep = Callable[..., Coroutine[Any, Any, None]]


def _get_workspace_id(request: Request) -> str:
    """Extract workspace ID from header or query param."""
    ws_id = request.headers.get("X-Workspace-Id") or request.query_params.get("workspace_id")
    if not ws_id:
        raise HTTPException(status_code=400, detail="Missing workspace ID")
    return ws_id


def _get_user_context(request: Request) -> dict[str, Any]:
    """Pull user context set by upstream AuthMiddleware."""
    ctx = getattr(request.state, "user_context", None)
    if ctx is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return ctx


def require_role(*roles: WorkspaceRole | str) -> _GuardDep:
    """FastAPI dependency -- checks workspace membership + role.

    Usage:
        @router.post("/agents", dependencies=[Depends(require_role("admin"))])
    """
    resolved = [r if isinstance(r, WorkspaceRole) else WorkspaceRole.from_str(r) for r in roles]
    minimum = min(resolved, key=lambda r: r.level)

    async def _guard(request: Request) -> None:
        _get_user_context(request)  # enforce authentication
        ws_id = _get_workspace_id(request)
        membership = getattr(request.state, "workspace_membership", None)
        if membership is None or membership.get("workspace_id") != ws_id:
            raise HTTPException(status_code=403, detail="Not a member of this workspace")
        try:
            check_workspace_role(membership.get("role", ""), minimum=minimum)
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc

    return _guard


def require_pocket_access(minimum: PocketAccess | str) -> _GuardDep:
    """FastAPI dependency -- checks pocket-level access."""
    resolved_min = minimum if isinstance(minimum, PocketAccess) else PocketAccess.from_str(minimum)

    async def _guard(request: Request, pocket_id: str) -> None:
        _get_user_context(request)  # enforce authentication
        pocket_membership = getattr(request.state, "pocket_membership", None)
        if pocket_membership is None or pocket_membership.get("pocket_id") != pocket_id:
            raise HTTPException(status_code=403, detail="No access to this pocket")
        try:
            check_pocket_access(pocket_membership.get("access", ""), minimum=resolved_min)
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc

    return _guard


def require_plan_feature(feature: str) -> _GuardDep:
    """FastAPI dependency -- checks workspace plan allows feature."""
    from pocketpaw_ee.guards.abac import PLAN_FEATURES

    async def _guard(request: Request) -> None:
        _get_user_context(request)  # enforce authentication
        plan = getattr(request.state, "workspace_plan", "team")
        allowed = PLAN_FEATURES.get(plan, set())
        if feature not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"plan.feature_denied: {feature!r} not available on {plan} plan",
            )

    return _guard


def require_policy(action: str) -> _GuardDep:
    """FastAPI dependency -- full ABAC evaluation."""

    async def _guard(request: Request) -> None:
        ctx = _get_user_context(request)
        ws_id = _get_workspace_id(request)
        membership = getattr(request.state, "workspace_membership", None)
        role_str = membership.get("role", "member") if membership else "member"

        # Agent ceiling: if an agent is acting, resolve its creator's role
        # from the agent_context populated by the agent execution middleware.
        agent_id = request.query_params.get("agent_id")
        agent_creator_role: WorkspaceRole | None = None
        if agent_id:
            agent_ctx = getattr(request.state, "agent_context", None)
            if agent_ctx and agent_ctx.get("agent_id") == agent_id:
                creator_role_str = agent_ctx.get("creator_role")
                if creator_role_str:
                    agent_creator_role = WorkspaceRole.from_str(creator_role_str)

        policy_ctx = PolicyContext(
            user_id=ctx.get("user_id", ""),
            workspace_id=ws_id,
            role=WorkspaceRole.from_str(role_str),
            action=action,
            resource_id=request.query_params.get("resource_id"),
            resource_type=request.query_params.get("resource_type"),
            plan=getattr(request.state, "workspace_plan", "team"),
            agent_id=agent_id,
            agent_creator_role=agent_creator_role,
        )

        result = evaluate_policy(policy_ctx)
        if not result.allowed:
            raise HTTPException(status_code=403, detail=result.code)

    return _guard


# ---------------------------------------------------------------------------
# User-model-backed helpers (for cloud routes using fastapi-users)
# ---------------------------------------------------------------------------
#
# The request.state-based guards above assume a middleware populates
# `request.state.workspace_membership`. Cloud routes authenticate via
# fastapi-users and carry the full User document with an embedded
# `workspaces: list[WorkspaceMembership]`. These helpers read that directly.
#
# To avoid importing `ee.cloud.*` (layering), helpers accept any object
# satisfying a duck-typed Protocol: `.workspaces` iterable of items with
# `.workspace: str` and `.role: str`.


class _HasWorkspaces:
    """Structural protocol — any object with a `workspaces` list of
    membership objects exposing `.workspace` and `.role` attributes."""

    workspaces: list[Any]


def resolve_workspace_role(user: Any, workspace_id: str) -> WorkspaceRole:
    """Return the user's WorkspaceRole for `workspace_id`. Raises Forbidden
    with code `workspace.not_member` if the user has no membership."""
    for m in getattr(user, "workspaces", []) or []:
        if getattr(m, "workspace", None) == workspace_id:
            return WorkspaceRole.from_str(getattr(m, "role", "member") or "member")
    raise Forbidden(
        code="workspace.not_member",
        detail=f"User is not a member of workspace {workspace_id}",
    )


def check_workspace_action(user: Any, workspace_id: str, action: str) -> WorkspaceRole:
    """Enforce an ACTIONS entry against a user's workspace role.

    Returns the resolved role on success. Raises Forbidden on deny and
    audits the denial. Use inside route handlers when the route needs the
    role value, otherwise prefer the factory deps below.
    """
    try:
        role = resolve_workspace_role(user, workspace_id)
        check_action(action, role)
    except Forbidden as exc:
        log_denial(
            actor=str(getattr(user, "id", "") or ""),
            action=action,
            code=exc.code,
            workspace_id=workspace_id,
            detail=exc.detail,
        )
        raise
    return role


def resolve_group_role(
    group: Any,
    user_id: str,
) -> GroupRole:
    """Derive a user's GroupRole from a Group document.

    - `group.owner == user_id`  → OWNER
    - `group.member_roles[user_id]` lookup (values: "admin"|"edit"|"view")
    - User in `group.members` with no explicit role → MEMBER (edit)
    - Otherwise → raises Forbidden `group.not_member`
    """
    if getattr(group, "owner", None) == user_id:
        return GroupRole.OWNER
    member_roles: dict[str, str] = getattr(group, "member_roles", {}) or {}
    if user_id in member_roles:
        return GroupRole.from_str(member_roles[user_id])
    members: list[str] = getattr(group, "members", []) or []
    if user_id in members:
        return GroupRole.MEMBER
    raise Forbidden(code="group.not_member", detail=f"User {user_id} not in group")


def check_group_action(group: Any, user_id: str, action: str) -> GroupRole:
    """Enforce a group-scoped ACTIONS entry. Returns resolved GroupRole."""
    try:
        role = resolve_group_role(group, user_id)
        rule: ActionRule = get_rule(action)
        # group.* actions may be keyed on GroupRole or WorkspaceRole
        # (e.g. group.create uses WorkspaceRole.MEMBER). Only enforce
        # group-role actions here; workspace-role actions on groups are
        # enforced via check_workspace_action upstream.
        if isinstance(rule.minimum, GroupRole):
            check_group_role(role, minimum=rule.minimum, deny_code=rule.deny_code)
    except Forbidden as exc:
        log_denial(
            actor=user_id,
            action=action,
            code=exc.code,
            resource_id=str(getattr(group, "id", "") or ""),
            detail=exc.detail,
        )
        raise
    return role


def make_require_action(
    action: str,
    user_dep: Callable[..., Any],
    workspace_dep: Callable[..., Any],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Build a FastAPI dependency enforcing `action` using the cloud's
    `current_active_user` + `current_workspace_id` dependencies.

    The cloud layer wires this up in `ee/cloud/shared/deps.py`:

        require_workspace_update = make_require_action(
            "workspace.update", current_active_user, current_workspace_id,
        )
        @router.patch(..., dependencies=[Depends(require_workspace_update)])

    Kept as a factory so the guards package stays decoupled from cloud auth.
    """
    from fastapi import Depends  # local import to keep top-level light

    async def _guard(
        user: Any = Depends(user_dep),
        workspace_id: str = Depends(workspace_dep),
    ) -> Any:
        try:
            check_workspace_action(user, workspace_id, action)
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        return user

    _guard.__name__ = f"require_action_{action.replace('.', '_')}"
    return _guard
