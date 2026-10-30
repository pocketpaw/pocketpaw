"""Per-user auth session row.

One document per minted JWT. The ``jti`` claim in the token is the
join key; revocation flips ``revoked`` here and also adds the jti to
the Redis set checked by :class:`RevocableJWTStrategy`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class AuthSession(Document):
    user_id: Indexed(str)  # type: ignore[valid-type]
    jti: Indexed(str, unique=True)  # type: ignore[valid-type]
    ip: str | None = None
    user_agent: str | None = None
    device_label: str = ""
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked: bool = False
    revoked_at: datetime | None = None

    class Settings:
        name = "auth_sessions"
        indexes = [
            [("user_id", 1), ("revoked", 1), ("issued_at", -1)],
            # 90-day TTL — abandoned rows GC themselves.
            IndexModel([("issued_at", 1)], expireAfterSeconds=90 * 86400),
        ]
