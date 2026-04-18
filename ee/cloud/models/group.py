"""Group document — multi-user channels with agent participants."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class GroupAgent(BaseModel):
    """Agent assigned to a group with a respond mode."""

    agent: str  # Agent ID
    role: str = "assistant"  # assistant | listener | moderator
    respond_mode: str = "mention_only"  # mention_only | auto | silent | smart


class Group(TimestampedDocument):
    """Chat group/channel — like Slack channels with AI agents."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    slug: str = ""
    description: str = ""
    icon: str = ""
    color: str = ""
    type: str = Field(default="public", pattern="^(public|private|dm)$")
    members: list[str] = Field(default_factory=list)  # User IDs
    agents: list[GroupAgent] = Field(default_factory=list)
    pinned_messages: list[str] = Field(default_factory=list)  # Message IDs
    owner: str  # User ID
    archived: bool = False
    last_message_at: datetime | None = None
    message_count: int = 0

    class Settings:
        name = "groups"
        indexes = [
            [("workspace", 1), ("slug", 1)],
        ]
