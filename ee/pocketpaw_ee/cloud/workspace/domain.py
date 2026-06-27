"""Domain value objects for the workspace module.

Frozen dataclasses, no Beanie / Pydantic / FastAPI imports:

- ``Workspace`` mirrors the persistence ``Workspace`` document plus a
  derived ``member_count`` that the service computes per request.
- ``Branding`` mirrors the optional per-tenant white-label branding
  embedded on the workspace (WB-1); ``Workspace.branding`` is None when
  the tenant has no custom branding.
- ``WorkspaceMember`` represents a user-as-member-of-a-workspace, the
  shape returned by ``list_members`` (the underlying data lives on the
  ``User`` document under ``user.workspaces``).
- ``Invite`` mirrors the persistence ``Invite`` document, with
  ``expired`` precomputed at the boundary so the domain entity stays
  pure (no clock dependency baked into a property).
- ``InviteContext`` mirrors the optional admin onboarding hints embedded
  on the invite (pp#1365); ``Invite.context`` is None when omitted.

2026-06-14 (WB-1): added the ``Branding`` value object and the
``Workspace.branding`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Branding:
    """Per-tenant white-label branding (WB-1).

    Mirrors the embedded persistence ``Branding`` shape. Every field is
    optional; an unset field falls back to the Paw default at render time
    (a frontend concern, not stored here)."""

    logo_asset: str | None = None
    favicon_asset: str | None = None
    display_name: str | None = None
    tab_title: str | None = None
    accent_color: str | None = None
    show_paw_mark: bool = True


@dataclass(frozen=True)
class Workspace:
    """A workspace (org/team) with a derived member-count."""

    id: str
    name: str
    slug: str
    owner: str  # user_id
    plan: str  # team | business | enterprise
    seats: int
    created_at: datetime
    member_count: int = 0
    deleted_at: datetime | None = None
    branding: Branding | None = None  # per-tenant white-label branding (WB-1)


@dataclass(frozen=True)
class WorkspaceMember:
    """A user's membership in a workspace, joined with their profile."""

    user_id: str
    email: str
    name: str
    avatar: str
    role: str  # owner | admin | member | viewer
    joined_at: datetime


@dataclass(frozen=True)
class VerifiedDomain:
    """A claimed email domain on a workspace. Mirrors the embedded
    persistence shape; DNS TXT verification flips ``verified``."""

    domain: str
    verification_token: str
    verified: bool
    verified_at: datetime | None
    auto_join: bool
    created_at: datetime


@dataclass(frozen=True)
class InviteContext:
    """Optional admin-provided onboarding hints carried on an invite.

    ``focus`` is a one-line description of what the new member will own;
    ``profile_pic`` is a reference to a suggested avatar. Both optional —
    the downstream VIP-onboarding flow reads whatever the admin supplied."""

    focus: str | None = None
    profile_pic: str | None = None


@dataclass(frozen=True)
class Invite:
    """A workspace invite. ``expired`` is computed by the repository at
    read time so the domain doesn't carry a clock dependency."""

    id: str
    workspace_id: str
    email: str
    role: str
    invited_by: str  # user_id
    token: str | None
    group_id: str | None
    accepted: bool
    revoked: bool
    expired: bool
    expires_at: datetime
    context: InviteContext | None = None  # optional admin onboarding hints (pp#1365)


__all__ = [
    "Branding",
    "Invite",
    "InviteContext",
    "VerifiedDomain",
    "Workspace",
    "WorkspaceMember",
]
