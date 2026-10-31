"""Domain value objects for auth.

Pure-Python frozen dataclasses, no Beanie / Pydantic / FastAPI imports.
The repository converts between these and the Beanie ``User`` document.
``WorkspaceMembershipRef`` mirrors the persistence ``WorkspaceMembership``
sub-model; both names exist temporarily during the transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WorkspaceMembershipRef:
    """Workspace membership entry on a User. Persistence-agnostic."""

    workspace: str
    role: str  # owner | admin | member | viewer
    joined_at: datetime


@dataclass(frozen=True)
class AuthUser:
    """Authenticated user, hydrated from the persistence layer.

    Tuples (not lists) for `workspaces` so the dataclass stays hashable
    and frozen-friendly.
    """

    id: str
    email: str
    full_name: str
    avatar: str
    status: str  # online | offline | away | dnd
    active_workspace: str | None
    workspaces: tuple[WorkspaceMembershipRef, ...]
    is_verified: bool
    is_superuser: bool
    mfa_enabled: bool = False
    # BYOK-first onboarding (2026-09-01): the frontend renders signup nudges
    # and upload blocks off this flag; it must survive a reload via /auth/me.
    is_guest: bool = False
    # Platform authority axis (2026-09-14) — the cross-tenant operator rung, or
    # None for the overwhelming majority of users. Carried on the domain object
    # because /auth/me is the only channel by which a client can learn it, and
    # it must not be inferred from is_superuser (a different, narrower thing).
    platform_role: str | None = None
    # Early access: when the one-time credit grant landed, and when the user
    # asked to hear about plans. None until each happens.
    early_access_granted_at: datetime | None = None
    notify_plans_at: datetime | None = None


@dataclass(frozen=True)
class UserIdentity:
    """The presentational half of a user — what a message header shows.

    Deliberately three fields and no more. It is handed to surfaces that attribute
    something to a person (a Paw Bar takeover reply, an audit line) and is meant to
    be safe to serialize outward to anyone already authorized to see WHO acted, so
    it carries no email, no membership, no status and no auth state.

    ``name`` is empty when the user has neither a full name nor an email, and the
    whole object is simply absent when the id resolves to nobody — an unresolvable
    author renders as anonymous rather than as a raw id.
    """

    id: str
    name: str
    avatar: str


@dataclass(frozen=True)
class PublicIdentity:
    """What an ANONYMOUS viewer may see of a user: a name and a picture.

    No id, no email. ``name`` is the trimmed ``full_name`` (never empty; a user
    without one resolves to nothing). ``avatar_url`` is the stored avatar only
    when it is an absolute http(s) URL, else ``None``: an uploaded picture is a
    backend-relative ``/uploads/...`` path a logged-out page can't load, and a
    ``data:`` URL is never served.
    """

    name: str
    avatar_url: str | None


__all__ = ["AuthUser", "PublicIdentity", "UserIdentity", "WorkspaceMembershipRef"]
