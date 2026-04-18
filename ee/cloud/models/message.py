"""Message document — group chat messages with mentions, reactions, threading."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class Mention(BaseModel):
    type: str = "user"  # user | agent | everyone
    id: str = ""  # User ID or Agent ID
    display_name: str = ""  # @rohit, @PocketPaw


class Attachment(BaseModel):
    type: str = "file"  # file | image | pocket | widget
    url: str = ""
    name: str = ""
    meta: dict = Field(default_factory=dict)


class Reaction(BaseModel):
    emoji: str
    users: list[str] = Field(default_factory=list)  # User IDs


class Message(TimestampedDocument):
    """Chat message in a group."""

    group: Indexed(str)  # type: ignore[valid-type]
    sender: str | None = None  # User ID, null = system message
    sender_type: str = "user"  # user | agent
    agent: str | None = None  # Agent ID when sender_type = "agent"
    content: str = ""
    mentions: list[Mention] = Field(default_factory=list)
    reply_to: str | None = None  # Parent message ID for threading
    attachments: list[Attachment] = Field(default_factory=list)
    reactions: list[Reaction] = Field(default_factory=list)
    edited: bool = False
    edited_at: datetime | None = None
    deleted: bool = False  # Soft delete

    class Settings:
        name = "messages"
        indexes = [
            [("group", 1), ("createdAt", -1)],
        ]
