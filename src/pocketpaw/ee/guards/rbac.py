# Core RBAC primitives — workspace roles, pocket access levels, and guard checks.
# Created: 2026-04-10

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Workspace roles — 3-tier hierarchy
# ---------------------------------------------------------------------------


class WorkspaceRole(StrEnum):
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def from_str(cls, value: str) -> WorkspaceRole:
        """Resolve a raw string to a WorkspaceRole (case-insensitive)."""
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unknown workspace role: {value!r}") from None

    @property
    def level(self) -> int:
        return _ROLE_LEVELS[self]


_ROLE_LEVELS: dict[WorkspaceRole, int] = {
    WorkspaceRole.MEMBER: 1,
    WorkspaceRole.ADMIN: 2,
    WorkspaceRole.OWNER: 3,
}


# ---------------------------------------------------------------------------
# Pocket access — 4-tier hierarchy
# ---------------------------------------------------------------------------


class PocketAccess(StrEnum):
    VIEW = "view"
    COMMENT = "comment"
    EDIT = "edit"
    OWNER = "owner"

    @classmethod
    def from_str(cls, value: str) -> PocketAccess:
        """Resolve a raw string to a PocketAccess (case-insensitive)."""
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unknown pocket access level: {value!r}") from None

    @property
    def level(self) -> int:
        return _ACCESS_LEVELS[self]


_ACCESS_LEVELS: dict[PocketAccess, int] = {
    PocketAccess.VIEW: 1,
    PocketAccess.COMMENT: 2,
    PocketAccess.EDIT: 3,
    PocketAccess.OWNER: 4,
}


# ---------------------------------------------------------------------------
# Forbidden exception
# ---------------------------------------------------------------------------


class Forbidden(Exception):
    """Authorization failure with machine-readable code."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Guard functions
# ---------------------------------------------------------------------------


def check_workspace_role(
    role: str | WorkspaceRole,
    *,
    minimum: WorkspaceRole,
) -> None:
    """Raise Forbidden if role is below minimum. Accepts raw strings from DB."""
    resolved = role if isinstance(role, WorkspaceRole) else WorkspaceRole.from_str(role)
    if resolved.level < minimum.level:
        raise Forbidden(
            code="workspace.insufficient_role",
            detail=f"Requires {minimum.value}, got {resolved.value}",
        )


def check_pocket_access(
    access: str | PocketAccess,
    *,
    minimum: PocketAccess,
) -> None:
    """Raise Forbidden if access is below minimum."""
    resolved = access if isinstance(access, PocketAccess) else PocketAccess.from_str(access)
    if resolved.level < minimum.level:
        raise Forbidden(
            code="pocket.insufficient_access",
            detail=f"Requires {minimum.value}, got {resolved.value}",
        )
