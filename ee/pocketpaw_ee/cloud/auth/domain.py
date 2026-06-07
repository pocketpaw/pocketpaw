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


__all__ = ["AuthUser", "WorkspaceMembershipRef"]
