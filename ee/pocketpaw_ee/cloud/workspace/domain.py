"""Domain value objects for the workspace module.

Three frozen dataclasses, no Beanie / Pydantic / FastAPI imports:

- ``Workspace`` mirrors the persistence ``Workspace`` document plus a
  derived ``member_count`` that the service computes per request.
- ``WorkspaceMember`` represents a user-as-member-of-a-workspace, the
  shape returned by ``list_members`` (the underlying data lives on the
  ``User`` document under ``user.workspaces``).
- ``Invite`` mirrors the persistence ``Invite`` document, with
  ``expired`` precomputed at the boundary so the domain entity stays
  pure (no clock dependency baked into a property).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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


__all__ = ["Invite", "VerifiedDomain", "Workspace", "WorkspaceMember"]
