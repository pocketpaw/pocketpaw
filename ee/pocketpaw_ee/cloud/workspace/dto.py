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
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.workspace.domain import Invite, VerifiedDomain, Workspace, WorkspaceMember

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


class BulkInviteRequest(BaseModel):
    """POST /workspaces/{id}/invites/bulk request.

    ``emails`` is bounded at 100 so a single batch can't dwarf the daily
    invite-rate budget. The frontend's paste-a-list UI clamps client-side.
    """

    emails: list[EmailStr] = Field(min_length=1, max_length=100)
    role: str = Field(default="member", pattern="^(admin|member)$")
    group_id: str | None = None


class BulkInviteSkip(BaseModel):
    """One per-email skip in the bulk response."""

    email: str
    reason: Literal["already_member", "already_pending", "invalid_email", "seat_limit"]


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
    token: str | None = None
    accepted: bool
    revoked: bool
    expired: bool
    expiresAt: str | None  # noqa: N815 - camelCase wire key

    model_config = {"populate_by_name": True}


class BulkInviteResponse(BaseModel):
    """POST /workspaces/{id}/invites/bulk response."""

    created: list[InviteOut]
    skipped: list[BulkInviteSkip]


class ValidateInviteOut(InviteOut):
    """GET /workspaces/invites/{token} response. Adds ``valid`` and
    ``workspace_name`` for the frontend's invite-landing page."""

    valid: bool
    workspace_name: str


class WorkspaceDeletePreviewResponse(BaseModel):
    """GET /workspaces/{id}/delete-preview response — blast-radius before delete.

    Counts the rows the cascade in ``workspace_service.delete`` will tear
    through (members, chat groups, agents, files, pending invites) plus the
    total file bytes attributable to the workspace. The UI uses this for a
    "Deleting will remove X members, Y rooms, Z bytes — this cannot be
    undone" confirmation step before the type-name-to-confirm prompt.
    """

    member_count: int
    room_count: int
    agent_count: int
    file_count: int
    invite_count: int
    total_bytes: int


class AddDomainRequest(BaseModel):
    """POST /workspaces/{id}/domains."""

    domain: str = Field(min_length=3, max_length=253)


class UpdateDomainRequest(BaseModel):
    """PATCH /workspaces/{id}/domains/{domain}."""

    auto_join: bool


class VerifiedDomainOut(BaseModel):
    """One verified-domain entry. ``verification_token`` is the value the
    admin must place in the domain's DNS TXT record before calling verify."""

    domain: str
    verification_token: str
    verified: bool
    verified_at: str | None = None
    auto_join: bool
    created_at: str | None = None


class InvitePreviewResponse(BaseModel):
    """Typed preview of an invite token for the accept UI.

    ``state`` is the single field the UI switches on:
      - ``ready_new``         — token is valid, viewer is anonymous; show register form
      - ``ready_existing``    — token is valid, viewer logged in with matching email
      - ``ready_wrong_user``  — token is valid, viewer logged in with a DIFFERENT email
      - ``expired``           — token expired
      - ``revoked``           — token revoked by inviter
      - ``already_accepted``  — token already redeemed
      - ``not_found``         — token doesn't exist (or was tampered)
    """

    state: Literal[
        "ready_new",
        "ready_existing",
        "ready_wrong_user",
        "expired",
        "revoked",
        "already_accepted",
        "not_found",
    ]
    email: str | None = None
    role: str | None = None
    workspace_name: str | None = None
    group: str | None = None
    group_name: str | None = None
    viewer_email: str | None = None


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


def verified_domain_to_dto(d: VerifiedDomain) -> VerifiedDomainOut:
    return VerifiedDomainOut(
        domain=d.domain,
        verification_token=d.verification_token,
        verified=d.verified,
        verified_at=iso_utc(d.verified_at),
        auto_join=d.auto_join,
        created_at=iso_utc(d.created_at),
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
    "AddDomainRequest",
    "BulkInviteRequest",
    "BulkInviteResponse",
    "BulkInviteSkip",
    "CreateInviteRequest",
    "CreateWorkspaceRequest",
    "InviteOut",
    "InvitePreviewResponse",
    "MemberOut",
    "UpdateDomainRequest",
    "UpdateMemberRoleRequest",
    "UpdateWorkspaceRequest",
    "ValidateInviteOut",
    "VerifiedDomainOut",
    "WorkspaceDeletePreviewResponse",
    "WorkspaceOut",
    "invite_to_dto",
    "invite_to_validate_dto",
    "member_to_dto",
    "verified_domain_to_dto",
    "workspace_to_dto",
]
