"""Wire DTOs for the auth domain.

Replaces ``ee/cloud/auth/schemas.py``. Field names match the existing
wire shape consumed by paw-enterprise (camelCase for ``emailVerified``,
``activeWorkspace``).
"""

from __future__ import annotations

from pydantic import BaseModel

from pocketpaw_ee.cloud.auth.domain import AuthUser


class ProfileUpdateRequest(BaseModel):
    """PATCH /auth/me request body."""

    full_name: str | None = None
    avatar: str | None = None
    status: str | None = None


class SetWorkspaceRequest(BaseModel):
    """POST /auth/set-active-workspace request body."""

    workspace_id: str


class WorkspaceMembershipDto(BaseModel):
    """Embedded workspace membership in a profile response."""

    workspace: str
    role: str


class ProfileOut(BaseModel):
    """GET /auth/me response. Field names are camelCase to match the
    existing wire shape."""

    id: str
    email: str
    name: str
    image: str
    emailVerified: bool  # noqa: N815 - intentional camelCase wire key
    activeWorkspace: str | None  # noqa: N815 - intentional camelCase wire key
    workspaces: list[WorkspaceMembershipDto]


def auth_user_to_profile_out(user: AuthUser) -> ProfileOut:
    return ProfileOut(
        id=user.id,
        email=user.email,
        name=user.full_name,
        image=user.avatar,
        emailVerified=user.is_verified,
        activeWorkspace=user.active_workspace,
        workspaces=[
            WorkspaceMembershipDto(workspace=m.workspace, role=m.role) for m in user.workspaces
        ],
    )


__all__ = [
    "ProfileOut",
    "ProfileUpdateRequest",
    "SetWorkspaceRequest",
    "WorkspaceMembershipDto",
    "auth_user_to_profile_out",
]
