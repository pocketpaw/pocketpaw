"""Session document — pocket-scoped chat sessions."""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Indexed
from pydantic import Field

from ee.cloud.models.base import TimestampedDocument


class Session(TimestampedDocument):
    """Chat session tracked by cloud, messages stored in Python.

    Field names use camelCase aliases to match the frontend contract.
    """

    sessionId: Indexed(str, unique=True) = Field(alias="sessionId")  # type: ignore[valid-type]
    pocket: str | None = None
    group: str | None = None
    agent: str | None = None
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    title: str = "New Chat"
    lastActivity: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="lastActivity")
    messageCount: int = Field(default=0, alias="messageCount")
    deleted_at: datetime | None = None

    model_config = {"populate_by_name": True}

    class Settings:
        name = "sessions"
        indexes = [
            [("workspace", 1), ("pocket", 1), ("lastActivity", -1)],
            [("workspace", 1), ("group", 1), ("agent", 1)],
        ]
