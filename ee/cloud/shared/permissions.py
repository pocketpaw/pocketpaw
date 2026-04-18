"""Role and access-level permission checks for the cloud module.

Provides enum hierarchies for workspace roles and pocket access levels,
plus guard functions that raise ``Forbidden`` when a user's level is
insufficient.
"""

from __future__ import annotations

from enum import Enum

from ee.cloud.shared.errors import Forbidden


class WorkspaceRole(Enum):
    """Workspace membership roles, ordered by privilege level."""

    MEMBER = ("member", 1)
    ADMIN = ("admin", 2)
    OWNER = ("owner", 3)

    def __init__(self, val: str, level: int) -> None:
        self._value_ = val
        self.level = level

    @classmethod
    def from_str(cls, role: str) -> WorkspaceRole:
        """Resolve a role string like ``"admin"`` to the enum member."""
        for member in cls:
            if member.value == role:
                return member
        raise ValueError(f"Unknown workspace role: {role!r}")


class PocketAccess(Enum):
    """Per-pocket access levels, ordered by privilege."""

    VIEW = ("view", 1)
    COMMENT = ("comment", 2)
    EDIT = ("edit", 3)
    OWNER = ("owner", 4)

    def __init__(self, val: str, level: int) -> None:
        self._value_ = val
        self.level = level

    @classmethod
    def from_str(cls, access: str) -> PocketAccess:
        """Resolve an access string like ``"edit"`` to the enum member."""
        for member in cls:
            if member.value == access:
                return member
        raise ValueError(f"Unknown pocket access level: {access!r}")


def check_workspace_role(role: str, *, minimum: str) -> None:
    """Raise ``Forbidden`` if *role* is below *minimum*."""
    actual = WorkspaceRole.from_str(role)
    required = WorkspaceRole.from_str(minimum)
    if actual.level < required.level:
        raise Forbidden(
            "workspace.insufficient_role",
            f"Requires {required.value} role, but user has {actual.value}",
        )


def check_pocket_access(access: str, *, minimum: str) -> None:
    """Raise ``Forbidden`` if *access* is below *minimum*."""
    actual = PocketAccess.from_str(access)
    required = PocketAccess.from_str(minimum)
    if actual.level < required.level:
        raise Forbidden(
            "pocket.insufficient_access",
            f"Requires {required.value} access, but user has {actual.value}",
        )
