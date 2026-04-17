"""Workspace document — one per deployment/org."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class WorkspaceSettings(BaseModel):
    default_agent: str | None = None  # Agent ID
    allow_invites: bool = True
    retention_days: int | None = None  # None = keep forever


class Workspace(TimestampedDocument):
    """Organization workspace — one per enterprise deployment."""

    name: str
    slug: Indexed(str, unique=True)  # type: ignore[valid-type]
    owner: str  # User ID (admin who created it)
    plan: str = "team"  # from license: team | business | enterprise
    seats: int = 5
    settings: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    deleted_at: datetime | None = None

    class Settings:
        name = "workspaces"
