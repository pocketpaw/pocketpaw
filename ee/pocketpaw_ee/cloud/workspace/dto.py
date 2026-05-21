"""Wire DTOs for the workspace domain.

Replaces ``ee/cloud/workspace/schemas.py``. Field names match the
existing wire shape consumed by paw-enterprise:
- ``_id`` (not ``id``) for entity identifiers
- ``createdAt``, ``expiresAt``, ``joinedAt`` (camelCase) for timestamps
- ``memberCount``, ``invitedBy`` (camelCase) for derived/foreign refs
- ``workspace_name``, ``valid`` for the validate-invite response
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember

# ---------------------------------------------------------------------------
# Requests (preserved from schemas.py)
# ---------------------------------------------------------------------------


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", v):
            raise ValueError("Slug must be lowercase alphanumeric with hyphens")
        return v


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    settings: dict | None = None


class CreateInviteRequest(BaseModel):
    email: str
    role: str = Field(default="member", pattern="^(admin|member)$")
    group_id: str | None = None


class UpdateMemberRoleRequest(BaseModel):
    role: str = Field(pattern="^(owner|admin|member)$")


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class WorkspaceOut(BaseModel):
    """GET /workspaces/{id} response."""

    id: str = Field(serialization_alias="_id")
    name: str
    slug: str
    owner: str
    plan: str
    seats: int
    createdAt: str | None  # noqa: N815 - camelCase wire key
    memberCount: int  # noqa: N815 - camelCase wire key

    model_config = {"populate_by_name": True}


class MemberOut(BaseModel):
    """A member entry returned by GET /workspaces/{id}/members."""

    id: str = Field(serialization_alias="_id")
    email: str
    name: str
    avatar: str
    role: str
    joinedAt: str | None  # noqa: N815 - camelCase wire key

    model_config = {"populate_by_name": True}


class InviteOut(BaseModel):
    """An invite entry."""

    id: str = Field(serialization_alias="_id")
    email: str
    role: str
    invitedBy: str  # noqa: N815 - camelCase wire key
    token: str
    accepted: bool
    revoked: bool
    expired: bool
    expiresAt: str | None  # noqa: N815 - camelCase wire key

    model_config = {"populate_by_name": True}


class ValidateInviteOut(InviteOut):
    """GET /workspaces/invites/{token} response. Adds ``valid`` and
    ``workspace_name`` for the frontend's invite-landing page."""

    valid: bool
    workspace_name: str


def workspace_to_dto(ws: Workspace) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        owner=ws.owner,
        plan=ws.plan,
        seats=ws.seats,
        createdAt=iso_utc(ws.created_at),
        memberCount=ws.member_count,
    )


def member_to_dto(m: WorkspaceMember) -> MemberOut:
    return MemberOut(
        id=m.user_id,
        email=m.email,
        name=m.name,
        avatar=m.avatar,
        role=m.role,
        joinedAt=iso_utc(m.joined_at),
    )


def invite_to_dto(inv: Invite) -> InviteOut:
    return InviteOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        invitedBy=inv.invited_by,
        token=inv.token,
        accepted=inv.accepted,
        revoked=inv.revoked,
        expired=inv.expired,
        expiresAt=iso_utc(inv.expires_at),
    )


def invite_to_validate_dto(inv: Invite, workspace_name: str) -> ValidateInviteOut:
    return ValidateInviteOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        invitedBy=inv.invited_by,
        token=inv.token,
        accepted=inv.accepted,
        revoked=inv.revoked,
        expired=inv.expired,
        expiresAt=iso_utc(inv.expires_at),
        valid=not (inv.accepted or inv.revoked or inv.expired),
        workspace_name=workspace_name,
    )


__all__ = [
    "CreateInviteRequest",
    "CreateWorkspaceRequest",
    "InviteOut",
    "MemberOut",
    "UpdateMemberRoleRequest",
    "UpdateWorkspaceRequest",
    "ValidateInviteOut",
    "WorkspaceOut",
    "invite_to_dto",
    "invite_to_validate_dto",
    "member_to_dto",
    "workspace_to_dto",
]
