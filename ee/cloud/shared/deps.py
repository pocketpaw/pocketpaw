"""FastAPI dependencies for cloud routers."""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ee.cloud.auth import current_active_user
from ee.cloud.models.user import User
from ee.cloud.shared.errors import Forbidden
from ee.cloud.shared.permissions import check_workspace_role


async def current_user(user: User = Depends(current_active_user)) -> User:
    """Get the authenticated user from JWT token."""
    return user


async def current_user_id(user: User = Depends(current_active_user)) -> str:
    """Extract user ID from JWT token."""
    return str(user.id)


async def current_workspace_id(user: User = Depends(current_active_user)) -> str:
    """Extract active workspace ID from the authenticated user."""
    if not user.active_workspace:
        raise HTTPException(400, "No active workspace. Create or join a workspace first.")
    return user.active_workspace


async def optional_workspace_id(user: User = Depends(current_active_user)) -> str | None:
    """Extract workspace ID if set, or None."""
    return user.active_workspace


def require_role(minimum: str):
    """Dependency factory: check user has minimum workspace role."""

    async def _check(
        user: User = Depends(current_active_user),
        workspace_id: str = Depends(current_workspace_id),
    ) -> User:
        membership = next(
            (w for w in user.workspaces if w.workspace == workspace_id),
            None,
        )
        if not membership:
            raise Forbidden("workspace.not_member", "Not a member of this workspace")
        check_workspace_role(membership.role, minimum=minimum)
        return user

    return _check
