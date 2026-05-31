"""Workspace-scoped API key document.

Each row stores the argon2 hash of the secret (the random tail after the
``paw_`` prefix); the plaintext is only returned to the caller on creation.
The first 8 hex chars after ``paw_`` are stored unhashed as ``prefix`` for
fast lookup on bearer auth.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class APIKey(Document):
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner_user_id: Indexed(str)  # type: ignore[valid-type]
    name: str
    prefix: Indexed(str)  # type: ignore[valid-type]
    hashed_secret: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "api_keys"
        indexes = [
            IndexModel([("prefix", 1), ("workspace", 1)]),
            IndexModel([("workspace", 1), ("revoked", 1)]),
        ]
