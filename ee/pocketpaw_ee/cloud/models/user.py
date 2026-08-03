"""User and OAuth account models (fastapi-users + Beanie).

Updated: 2026-08-01 (AM-6) — added ``OAuthAccount.linked_at`` so the
connected-accounts panel can say WHEN an identity was attached. Nullable with
no default rather than ``default_factory=now``: rows written before this field
existed have no honest timestamp, and stamping them with "now" on first read
would invent one. They read back as ``None`` and the UI omits the date.

Updated: 2026-05-21 — added ``home_pocket_id`` so the home page can be
backed by a per-user "home pocket". Optional (default None) — the auth
service resolves-or-provisions it lazily; existing users read back as
"no home pocket yet".
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from fastapi_users_db_beanie import BaseOAuthAccount, BeanieBaseUser
from pydantic import BaseModel, Field


class OAuthAccount(BaseOAuthAccount):
    """OAuth account linked to a User (Google, GitHub, etc.).

    ``linked_at`` is ours, not fastapi-users'. See the module docstring for why
    it is nullable instead of defaulting to now.
    """

    linked_at: datetime | None = None


class WorkspaceMembership(BaseModel):
    workspace: str  # Workspace ID
    role: str = "member"  # owner | admin | member | viewer
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class User(BeanieBaseUser, Document):  # type: ignore[misc]
    """Enterprise user with OAuth support."""

    full_name: str = ""
    avatar: str = ""
    active_workspace: str | None = None  # Current workspace ID
    # Id of the user's "home pocket" — the Pocket that backs the home page.
    # Resolved-or-provisioned lazily by ``pockets.service.ensure_home_pocket``.
    home_pocket_id: str | None = None
    workspaces: list[WorkspaceMembership] = Field(default_factory=list)
    status: str = Field(default="offline", pattern="^(online|offline|away|dnd)$")
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    oauth_accounts: list[OAuthAccount] = Field(default_factory=list)

    # MFA / TOTP state (Wave 3 Task 3). pending_setup + enabled form a
    # tri-state: (False, False) never set up; (True, False) secret minted
    # but not yet verified; (*, True) active. Backup codes stored as
    # sha256 of the plaintext "xxxx-xxxx" form.
    mfa_totp_secret: str | None = None
    mfa_enabled: bool = False
    mfa_backup_codes: list[str] = Field(default_factory=list)
    mfa_verified_at: datetime | None = None
    mfa_pending_setup: bool = False

    class Settings:
        name = "users"
        email_collation = None
