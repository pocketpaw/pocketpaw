"""Comment document — threaded comments on pockets."""

from __future__ import annotations

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class CommentTarget(BaseModel):
    type: str = Field(pattern="^(pocket|widget|agent)$")
    pocket_id: str
    widget_id: str | None = None


class CommentAuthor(BaseModel):
    id: str
    name: str
    avatar: str = ""


class Comment(TimestampedDocument):
    """Threaded comment on a pocket or widget."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    target: CommentTarget
    thread: str | None = None  # Parent comment ID for replies
    author: CommentAuthor
    body: str
    mentions: list[str] = Field(default_factory=list)  # User IDs
    resolved: bool = False
    resolved_by: str | None = None

    class Settings:
        name = "comments"
        indexes = [
            [("target.pocket_id", 1), ("created_at", -1)],
        ]
