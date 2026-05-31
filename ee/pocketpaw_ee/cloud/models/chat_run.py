"""Beanie document for one assistant chat turn."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from beanie import Document
from pydantic import Field
from pymongo import IndexModel

RunStatus = Literal["queued", "running", "completed", "interrupted", "failed", "cancelled"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChatRunDoc(Document):
    run_id: str
    workspace: str
    context_type: str  # "dm" | "group" | "pocket" | "session"
    scope_id: str
    session_key: str
    group: str | None = None
    user_id: str
    agent_id: str
    client_message_id: str
    user_message_id: str
    assistant_message_id: str | None = None
    status: RunStatus = "queued"
    partial_text: str = ""
    error: str | None = None
    createdAt: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    class Settings:
        name = "chat_runs"
        # Uniques close the create_run find-then-insert race.
        indexes = [
            IndexModel([("run_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("client_message_id", 1)], unique=True),
            [("workspace", 1), ("context_type", 1), ("scope_id", 1), ("createdAt", -1)],
        ]
