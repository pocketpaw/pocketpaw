"""Base document with automatic createdAt/updatedAt timestamps."""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Insert, Replace, Save, Update, before_event
from pydantic import Field


class TimestampedDocument(Document):
    """Base document that auto-manages createdAt and updatedAt fields."""

    createdAt: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updatedAt: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @before_event(Insert)
    def _set_created(self):
        now = datetime.now(UTC)
        self.createdAt = now
        self.updatedAt = now

    @before_event(Replace, Save, Update)
    def _set_updated(self):
        self.updatedAt = datetime.now(UTC)

    class Settings:
        use_state_management = True
