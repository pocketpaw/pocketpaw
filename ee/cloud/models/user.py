"""User and OAuth account models (fastapi-users + Beanie)."""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from fastapi_users_db_beanie import BaseOAuthAccount, BeanieBaseUser
from pydantic import BaseModel, Field


class OAuthAccount(BaseOAuthAccount):
    """OAuth account linked to a User (Google, GitHub, etc.)."""

    pass


class WorkspaceMembership(BaseModel):
    workspace: str  # Workspace ID
    role: str = "member"  # owner | admin | member | viewer
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class User(BeanieBaseUser, Document):  # type: ignore[misc]
    """Enterprise user with OAuth support."""

    full_name: str = ""
    avatar: str = ""
    active_workspace: str | None = None  # Current workspace ID
    workspaces: list[WorkspaceMembership] = Field(default_factory=list)
    status: str = Field(default="offline", pattern="^(online|offline|away|dnd)$")
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    oauth_accounts: list[OAuthAccount] = Field(default_factory=list)

    class Settings:
        name = "users"
        email_collation = None
