"""Auth domain — request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel


class ProfileUpdateRequest(BaseModel):
    full_name: str | None = None
    avatar: str | None = None
    status: str | None = None


class SetWorkspaceRequest(BaseModel):
    workspace_id: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    image: str
    email_verified: bool
    active_workspace: str | None
    workspaces: list[dict]
    model_config = {"from_attributes": True}
