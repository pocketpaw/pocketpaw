---
{
  "title": "Core RBAC Primitives: Workspace Roles, Pocket Access Levels, and Forbidden Exception",
  "summary": "`rbac.py` establishes the foundational enums and exception for PocketPaw's role-based access control: a 3-tier `WorkspaceRole` hierarchy, a 4-tier `PocketAccess` hierarchy, and the `Forbidden` exception that carries machine-readable denial codes. All other guard modules depend on these primitives without circular imports.",
  "concepts": [
    "WorkspaceRole",
    "PocketAccess",
    "Forbidden exception",
    "StrEnum",
    "level comparison",
    "least-privileged default",
    "check_workspace_role",
    "check_pocket_access",
    "RBAC primitives"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "authorization",
    "RBAC"
  ],
  "source_docs": [
    "7f01917ee44ccb53"
  ],
  "backlinks": null,
  "word_count": 507,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`rbac.py` (`src/pocketpaw/ee/guards/rbac.py`) is the lowest-level module in the guards stack. It defines the authorization vocabulary that every other guard module imports and operates on. Its only dependency is `enum` from the standard library, making it safe to import anywhere without risking circular imports.

## WorkspaceRole

```python
class WorkspaceRole(StrEnum):
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def from_str(cls, value: str) -> WorkspaceRole:
        try:
            return cls(value.lower())
        except ValueError:
            return cls.MEMBER  # default to least-privileged on unknown

    @property
    def level(self) -> int:
        return {"member": 0, "admin": 1, "owner": 2}[self.value]
```

The three-tier hierarchy (MEMBER < ADMIN < OWNER) covers the vast majority of workspace authorization needs. Using `StrEnum` means role values serialize directly to JSON strings without a custom encoder, and comparisons work with raw string values from the database.

`from_str()` defaults to `MEMBER` on unknown input rather than raising. This is a **least-privileged default**: if a user's role is stored as an unrecognized string (e.g., from a schema migration), they get minimal access rather than denied access entirely. The tradeoff is that a misconfigured role field silently grants member access — operators should monitor for unexpected `MEMBER` resolutions in audit logs.

The `level` property enables numeric comparison (`role.level >= minimum.level`) rather than using `==`, which would require exhaustive enum comparisons for hierarchical checks.

## PocketAccess

```python
class PocketAccess(StrEnum):
    VIEW = "view"
    COMMENT = "comment"
    EDIT = "edit"
    OWNER = "owner"
```

`PocketAccess` is a separate four-tier hierarchy for pocket-level permissions, independent of workspace role. A workspace MEMBER might have EDIT access to one pocket and VIEW access to another. This separation allows fine-grained sharing without changing the user's workspace role.

## Forbidden Exception

```python
class Forbidden(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
```

`Forbidden` carries two fields: `code` (the stable machine-readable string the frontend uses) and `detail` (human-readable context for logs). Separating these fields prevents the temptation to parse the exception message string for the code — a pattern that breaks whenever message formatting changes.

All guard functions raise `Forbidden` internally; the FastAPI integration layer in `deps.py` catches it and converts it to `HTTPException(403, detail=exc.code)`. This separation keeps the RBAC logic free of FastAPI imports.

## check_workspace_role and check_pocket_access

Both check functions follow the same pattern: resolve the input string to the enum, compare levels, raise `Forbidden` on failure:

```python
def check_workspace_role(role: str | WorkspaceRole, *, minimum: WorkspaceRole) -> None:
    resolved = role if isinstance(role, WorkspaceRole) else WorkspaceRole.from_str(role)
    if resolved.level < minimum.level:
        raise Forbidden(
            code="workspace.insufficient_role",
            detail=f"Requires {minimum.value}, got {resolved.value}",
        )
```

Accepting `str | WorkspaceRole` means callers can pass raw database values without pre-converting them, reducing boilerplate at call sites.

## Known Gaps

- `WorkspaceRole.from_str()` defaults to MEMBER on unknown input. There is no warning or log at this point, so a corrupt role field in the database would grant silent minimum access without any observable signal.
- `PocketAccess` has no `from_str()` default fallback — it raises `ValueError` on unknown input, which is more correct for pocket access but inconsistent with the workspace role behavior.