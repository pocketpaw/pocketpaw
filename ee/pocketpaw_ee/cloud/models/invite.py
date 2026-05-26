"""Invite document — workspace membership invitations.

The plaintext token lives only in the email link the inviter shares.
We persist sha256(plaintext) so a DB read cannot reconstruct a usable
invite link. ``token`` is the legacy plaintext column kept Optional for
backfill during the hashing rollout — new invites set ``token_hash``
and leave ``token`` as None.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


def _default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=7)


def hash_token(plaintext: str) -> str:
    """sha256(plaintext) — the canonical lookup value for an invite token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class Invite(Document):
    """Workspace invitation sent to an email address.

    ``token_hash`` is the authoritative lookup key. ``token`` is the
    legacy plaintext column retained Optional for one release so
    pre-hash invites keep working — backfilled by the service on first
    read.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    email: Indexed(str)  # type: ignore[valid-type]
    role: str = Field(default="member", pattern="^(admin|member|viewer)$")
    invited_by: str
    token: str | None = None  # legacy plaintext (deprecated; nulled after migration)
    token_hash: Indexed(str, unique=True) | None = None  # type: ignore[valid-type]
    group: str | None = None
    accepted: bool = False
    revoked: bool = False
    revoked_reason: str | None = None  # e.g. "declined" when invitee declines vs inviter-revoke
    accepted_at: datetime | None = None  # single-use stamp (Task 4)
    expires_at: datetime = Field(default_factory=_default_expiry)
    resend_count: int = 0  # increments on each POST /invites/{id}/resend

    @property
    def expired(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return datetime.now(UTC) > exp

    class Settings:
        name = "invites"
        indexes = [
            # Mongo auto-deletes documents whose expires_at is more than 14
            # days in the past — gives the application a 7-day grace beyond
            # the 7-day invite expiry for late accepts / audit, then GC's.
            IndexModel([("expires_at", 1)], expireAfterSeconds=86400 * 14),
        ]
