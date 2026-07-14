"""Invite document — workspace membership invitations.

The plaintext token lives only in the email link the inviter shares.
We persist sha256(plaintext) so a DB read cannot reconstruct a usable
invite link. ``token`` is the legacy plaintext column kept Optional for
backfill during the hashing rollout — new invites set ``token_hash``
and leave ``token`` as None.

2026-06-08 (pp#1365): added the optional embedded ``InviteContext`` and the
nullable ``context`` field so an invite can carry admin-provided onboarding
hints (``focus`` + ``profile_pic``) for a later VIP-onboarding flow. Fully
optional — pre-existing invite rows read back with ``context=None``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from beanie import Document, Indexed
from pydantic import BaseModel, Field
from pymongo import IndexModel


def _default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=7)


class InviteContext(BaseModel):
    """Optional admin-provided onboarding hints carried on an invite.

    Both fields are optional so the admin can supply either, both, or
    neither. ``focus`` is a one-line description of what the new member
    will own; ``profile_pic`` is a reference (e.g. an uploaded file id)
    to a suggested avatar. Consumed by the downstream VIP-onboarding flow.
    """

    focus: str | None = None
    profile_pic: str | None = None


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
    context: InviteContext | None = None  # optional admin onboarding hints (pp#1365)
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


# ---------------------------------------------------------------------------
# Meeting Invite — shareable links to join a LiveKit call as a guest
# ---------------------------------------------------------------------------


def _meeting_invite_default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=24)


class MeetingInvite(Document):
    """A shareable invite link that lets external guests join a LiveKit call.

    The plaintext token lives only in the shared URL. ``token_hash`` is
    ``sha256(plaintext)`` — the authoritative lookup key. Guests join with a
    temporary ``guest-`` prefixed identity and a LiveKit access token that
    expires after the call window.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    group_id: Indexed(str)  # type: ignore[valid-type]
    room_name: str
    token_hash: Indexed(str, unique=True) | None = None  # type: ignore[valid-type]
    created_by: str  # user_id
    display_name: str = ""  # human label shown in the invite list
    max_uses: int = 0  # 0 = unlimited
    use_count: int = 0
    guest_identities: list[str] = Field(default_factory=list)
    revoked: bool = False
    expires_at: datetime = Field(default_factory=_meeting_invite_default_expiry)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def expired(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return datetime.now(UTC) > exp

    @property
    def exhausted(self) -> bool:
        """True when max_uses > 0 and use_count >= max_uses."""
        return self.max_uses > 0 and self.use_count >= self.max_uses

    class Settings:
        name = "meeting_invites"
        indexes = [
            IndexModel([("expires_at", 1)], expireAfterSeconds=86400 * 14),
            [("group_id", 1), ("revoked", 1)],
        ]
