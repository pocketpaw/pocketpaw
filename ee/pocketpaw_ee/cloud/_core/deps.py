"""Cross-cutting FastAPI dependencies for cloud routers.

These deps belong to no single domain: they extract identity and
workspace from the authed user, and enforce workspace-level role-based
access control. Domain-specific guards (group, agent, pocket) live in
their owning modules; until those modules migrate, they remain in
``ee.cloud.shared.deps``.

The action-based guard machinery (``require_action``, ``require_membership``)
delegates to ``pocketpaw.ee.guards`` (the platform-wide RBAC package) for
the actual policy lookup. We translate platform ``GuardForbidden``
exceptions to cloud-native ``Forbidden`` so the standard error envelope
applies.

Changes: added require_plan_feature dependency for plan-tier feature gating.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.guards.audit import log_denial
from pocketpaw_ee.guards.deps import check_workspace_action
from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

# ---------------------------------------------------------------------------
# Identity / workspace extraction
# ---------------------------------------------------------------------------


async def current_user(user: User = Depends(current_active_user)) -> User:
    """Get the authenticated user from the JWT/session."""
    return user


async def current_user_id(user: User = Depends(current_active_user)) -> str:
    """Extract user ID from the authenticated user."""
    return str(user.id)


async def current_workspace_id(user: User = Depends(current_active_user)) -> str:
    """Extract active workspace ID from the authenticated user.

    Raises HTTP 400 (not a CloudError — this surfaces as a setup error
    that the client UI handles, not a denial) when the user has no
    active workspace.
    """
    if not user.active_workspace:
        raise HTTPException(400, "No active workspace. Create or join a workspace first.")
    return user.active_workspace


async def optional_workspace_id(
    user: User = Depends(current_active_user),
) -> str | None:
    """Extract workspace ID if set, or None."""
    return user.active_workspace


# ---------------------------------------------------------------------------
# Action-based guards (workspace scope)
# ---------------------------------------------------------------------------


async def _workspace_id_from_path(workspace_id: str) -> str:
    """Pull `workspace_id` from the path. FastAPI binds by parameter name."""
    return workspace_id


_WorkspaceIdDep = Callable[..., Coroutine[Any, Any, str]]


def require_action(
    action: str,
    workspace_dep: _WorkspaceIdDep = _workspace_id_from_path,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """FastAPI dependency enforcing an ACTIONS entry against the caller's
    workspace role.

    Default ``workspace_dep`` reads ``workspace_id`` from the path. Pass
    ``current_workspace_id`` to read from the user's active workspace
    instead.

    On deny, raises the cloud-native ``Forbidden`` (CloudError) so the
    global exception handler emits the standard error envelope. Every
    denial is audited via ``log_denial`` already inside
    ``check_workspace_action``, but we also surface the guard's ``code``
    through the cloud envelope.
    """

    async def _guard(
        user: User = Depends(current_active_user),
        workspace_id: str = Depends(workspace_dep),
    ) -> User:
        try:
            check_workspace_action(user, workspace_id, action)
        except GuardForbidden as exc:
            raise Forbidden(exc.code, exc.detail or "Access denied") from exc
        return user

    _guard.__name__ = f"require_action_{action.replace('.', '_')}"
    return _guard


def require_action_any_workspace(
    action: str,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Variant of ``require_action`` that resolves workspace from the
    user's ``active_workspace``. Use when the route has no
    ``{workspace_id}`` path param."""
    return require_action(action, workspace_dep=current_workspace_id)


async def require_membership(
    user: User = Depends(current_active_user),
    workspace_id: str = Depends(_workspace_id_from_path),
) -> User:
    """Light guard — just asserts the user is a member of the path
    workspace. Used on read routes where any member can view (no role
    check)."""
    for m in user.workspaces:
        if m.workspace == workspace_id:
            return user
    log_denial(
        actor=str(user.id),
        action="workspace.view",
        code="workspace.not_member",
        workspace_id=workspace_id,
    )
    raise Forbidden("workspace.not_member", "Not a member of this workspace")


# ---------------------------------------------------------------------------
# Plan-tier feature gate
# ---------------------------------------------------------------------------

_PLAN_ORDER = ("team", "business", "enterprise")


def require_plan_feature(feature: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """FastAPI dependency gating access by workspace plan tier.

    Loads the workspace's plan from the Workspace document and checks it
    against PLAN_FEATURES. Raises cloud Forbidden with code
    'plan.feature_denied' if the feature is not available on the plan.

    Use on routes that require business+ or enterprise features::

        @router.get(
            "/fabric/types",
            dependencies=[
                Depends(require_plan_feature("fabric")),
                Depends(require_action_any_workspace("fabric.read")),
            ],
        )
    """
    from pocketpaw_ee.guards.abac import PLAN_FEATURES

    # Compute the minimum plan that unlocks this feature, for the error message.
    needed_plan = "enterprise"
    for plan in _PLAN_ORDER:
        if feature in PLAN_FEATURES.get(plan, set()):
            needed_plan = plan
            break

    async def _guard(workspace_id: str = Depends(current_workspace_id)) -> None:
        from pocketpaw_ee.cloud.workspace import service as workspace_service

        # Re-raises on DB errors — better to surface 5xx than silently
        # downgrade an enterprise customer to the most restrictive plan
        # during a transient Mongo flap.
        plan = await workspace_service.get_workspace_plan(workspace_id)
        if plan is None:
            raise NotFound("workspace", workspace_id)
        allowed_features = PLAN_FEATURES.get(plan, set())
        if feature not in allowed_features:
            raise Forbidden(
                "plan.feature_denied",
                f"feature {feature!r} requires {needed_plan}",
            )

    _guard.__name__ = f"require_plan_feature_{feature.replace('.', '_')}"
    return _guard


__all__ = [
    "current_user",
    "current_user_id",
    "current_workspace_id",
    "optional_workspace_id",
    "require_action",
    "require_action_any_workspace",
    "require_membership",
    "require_plan_feature",
]
