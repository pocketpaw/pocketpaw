"""Workspace domain — Pydantic request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Requests
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


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    slug: str
    owner: str
    plan: str
    seats: int
    created_at: datetime
    member_count: int = 0


class MemberResponse(BaseModel):
    id: str
    email: str
    name: str
    avatar: str
    role: str
    joined_at: datetime


class InviteResponse(BaseModel):
    id: str
    email: str
    role: str
    invited_by: str
    token: str
    accepted: bool
    revoked: bool
    expired: bool
    expires_at: datetime
