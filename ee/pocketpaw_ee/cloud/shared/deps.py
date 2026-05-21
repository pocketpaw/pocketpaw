"""FastAPI dependencies for cloud routers.

The cross-cutting deps (identity extraction and workspace-level action
guards) moved to ``ee.cloud._core.deps`` in Phase 1 of the
cloud-restructure (2026-04-27). They are re-exported here so existing
imports keep working; new code should import from ``_core``.

The domain-specific guards (group, agent, pocket) remain here. They
delegate the Beanie loads to their owning entity services so this
module doesn't import any ``ee.cloud.models.*`` Beanie docs.

Changes: re-export require_plan_feature from _core.deps.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends

from pocketpaw_ee.cloud._core.deps import (
    _workspace_id_from_path,
    current_user,
    current_user_id,
    current_workspace_id,
    optional_workspace_id,
    require_action,
    require_action_any_workspace,
    require_membership,
    require_plan_feature,
)
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.guards.audit import log_denial
from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden

__all__ = [
    "_workspace_id_from_path",
    "current_user",
    "current_user_id",
    "current_workspace_id",
    "optional_workspace_id",
    "require_action",
    "require_action_any_workspace",
    "require_agent_owner_or_admin",
    "require_group_action",
    "require_membership",
    "require_plan_feature",
    "require_pocket_edit",
    "require_pocket_owner",
]


# ---------------------------------------------------------------------------
# Domain-specific guards — Beanie loads delegated to entity services
# ---------------------------------------------------------------------------


def require_group_action(action: str) -> Callable[..., Coroutine[Any, Any, Any]]:
    """FastAPI dependency enforcing a group-scoped action.

    Resolves the caller's ``GroupRole`` via
    ``chat.group_service.resolve_role_for_id`` and checks the ACTIONS
    rule for ``action``. Raises cloud ``Forbidden`` on deny.
    """
    from pocketpaw_ee.guards.actions import GroupRole, get_rule

    rule = get_rule(action)

    async def _guard(
        group_id: str,
        user: Any = Depends(current_active_user),
    ) -> Any:
        from pocketpaw_ee.cloud.chat import group_service

        try:
            role = await group_service.resolve_role_for_id(group_id, str(user.id))
            if isinstance(rule.minimum, GroupRole) and role.level < rule.minimum.level:
                log_denial(
                    actor=str(user.id),
                    action=action,
                    code=rule.deny_code,
                    resource_id=group_id,
                )
                raise GuardForbidden(
                    code=rule.deny_code,
                    detail=f"Requires {rule.minimum.value}, got {role.value}",
                )
        except GuardForbidden as exc:
            raise Forbidden(exc.code, exc.detail or "Access denied") from exc
        return user

    _guard.__name__ = f"require_group_action_{action.replace('.', '_')}"
    return _guard


# ---------------------------------------------------------------------------
# Agent-scoped action guard (owner OR workspace admin)
# ---------------------------------------------------------------------------


async def require_agent_owner_or_admin(
    agent_id: str,
    user: Any = Depends(current_active_user),
) -> Any:
    """Allow the action if the caller is the agent's owner OR a workspace
    admin (or owner) of the agent's workspace.

    Raises cloud ``Forbidden`` with ``agent.not_owner`` if neither.
    """
    from pocketpaw_ee.cloud.agents import service as agents_service

    if await agents_service.is_owner_or_workspace_admin(agent_id, user):
        return user

    log_denial(
        actor=str(user.id),
        action="agent.edit",
        code="agent.not_owner",
        resource_id=agent_id,
        workspace_id=await agents_service.get_workspace(agent_id),
    )
    raise Forbidden("agent.not_owner", "Only the agent owner or a workspace admin may do this")


# ---------------------------------------------------------------------------
# Pocket-scoped action guards
# ---------------------------------------------------------------------------


async def require_pocket_edit(
    pocket_id: str,
    user: Any = Depends(current_active_user),
) -> Any:
    """Allow if the caller is pocket.owner, in pocket.shared_with, or the
    pocket is workspace-visible. Mirrors the service-level edit-access
    check so the guard fails closed before the handler runs.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    user_id = str(user.id)
    if await pockets_service.has_edit_access(pocket_id, user_id):
        return user

    log_denial(
        actor=user_id,
        action="pocket.edit",
        code="pocket.access_denied",
        resource_id=pocket_id,
    )
    raise Forbidden("pocket.access_denied", "You do not have edit access to this pocket")


async def require_pocket_owner(
    pocket_id: str,
    user: Any = Depends(current_active_user),
) -> Any:
    """Allow only the pocket owner. Used for share-link, delete, and
    collaborator mutations."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    if await pockets_service.is_owner(pocket_id, str(user.id)):
        return user

    log_denial(
        actor=str(user.id),
        action="pocket.share",
        code="pocket.not_owner",
        resource_id=pocket_id,
    )
    raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")
