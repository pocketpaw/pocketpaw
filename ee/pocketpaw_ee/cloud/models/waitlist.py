# ee/pocketpaw_ee/cloud/models/waitlist.py: one email captured while early-access
# signups are paused (``waitlist`` collection). Unique on the lowercased email, so
# joining twice is a no-op. Only ``ee.cloud.early_access.service`` writes it.

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


class WaitlistEntry(Document):
    """An email that asked to be told when early access opens again."""

    email: str
    source: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "waitlist"
        indexes = [IndexModel([("email", 1)], unique=True, name="uq_waitlist_email")]


__all__ = ["WaitlistEntry"]
