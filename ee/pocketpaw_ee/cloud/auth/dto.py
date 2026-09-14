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
    mfa_enabled: bool  # snake_case: matches the paw-enterprise auth types wire key
    # snake_case wire key, frozen with the BYOK-fe sibling (2026-09-01): guests
    # get signup nudges + upload blocks; the flag must survive a page reload.
    is_guest: bool = False
    # Platform authority axis (2026-09-14). snake_case, matching mfa_enabled and
    # is_guest above rather than the older camelCase keys.
    #
    # This is the ONLY way the frontend can tell a platform operator from any
    # other signed-in user: is_superuser is not on this wire at all, and a
    # workspace role says nothing about platform access. Paw Admin reads this
    # field and nothing else to decide whether to render the console.
    #
    # Null for every user until an operator is granted one. Sent as null rather
    # than omitted so a client can distinguish "no platform access" from "this
    # server is too old to have the field".
    platform_role: str | None = None


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
        mfa_enabled=user.mfa_enabled,
        is_guest=user.is_guest,
        platform_role=user.platform_role,
    )


__all__ = [
    "ProfileOut",
    "ProfileUpdateRequest",
    "SetWorkspaceRequest",
    "WorkspaceMembershipDto",
    "auth_user_to_profile_out",
]
