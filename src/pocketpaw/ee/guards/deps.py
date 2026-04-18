# FastAPI dependency factories — RBAC/ABAC guard injection for route handlers.
# Created: 2026-04-10

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request

from pocketpaw.ee.guards.abac import evaluate_policy
from pocketpaw.ee.guards.policy import PolicyContext
from pocketpaw.ee.guards.rbac import (
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
    from pocketpaw.ee.guards.abac import PLAN_FEATURES

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
