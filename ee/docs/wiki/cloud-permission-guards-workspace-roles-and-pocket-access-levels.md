---
{
  "title": "Cloud Permission Guards: Workspace Roles and Pocket Access Levels",
  "summary": "Defines ordered enum hierarchies for workspace roles (member/admin/owner) and pocket access levels (view/comment/edit/owner), along with guard functions that raise Forbidden when a user lacks sufficient privilege. This module centralizes authorization checks so that every service method can enforce permissions with a single function call.",
  "concepts": [
    "WorkspaceRole",
    "PocketAccess",
    "check_workspace_role",
    "check_pocket_access",
    "Forbidden",
    "RBAC",
    "permission guard",
    "enum hierarchy"
  ],
  "categories": [
    "authorization",
    "cloud",
    "security",
    "shared utilities"
  ],
  "source_docs": [
    "1ab49c4e7d6a56f0"
  ],
  "backlinks": null,
  "word_count": 381,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cloud Permission Guards: Workspace Roles and Pocket Access Levels

## Purpose

This module exists to provide a single, consistent way to check whether a user has sufficient privilege to perform an operation. Rather than scattering role-comparison logic across every service method, the codebase centralizes it here into two enum classes and two guard functions.

## Architecture

### WorkspaceRole Enum

Three-tier hierarchy with numeric levels:

| Role | Level | Typical Use |
|------|-------|-------------|
| `MEMBER` | 1 | Read access, basic participation |
| `ADMIN` | 2 | Manage members, update workspace settings |
| `OWNER` | 3 | Delete workspace, transfer ownership |

Each enum member stores both a string value (for serialization) and a numeric level (for comparison). The `from_str` classmethod resolves raw strings from the database or API payloads into typed enum members, raising `ValueError` on unknown roles to prevent silent failures from typos or corrupted data.

### PocketAccess Enum

Four-tier hierarchy for per-pocket granularity:

| Access | Level |
|--------|-------|
| `VIEW` | 1 |
| `COMMENT` | 2 |
| `EDIT` | 3 |
| `OWNER` | 4 |

### Guard Functions

- `check_workspace_role(role, *, minimum)` — Compares the user's role level against the required minimum. Raises `Forbidden` with a structured error code (`workspace.insufficient_role`) if insufficient.
- `check_pocket_access(access, *, minimum)` — Same pattern for pocket-level access, with error code `pocket.insufficient_access`.

Both functions accept raw strings (not enum members), which means they handle the string-to-enum conversion internally. This keeps callers clean — they pass the role string straight from the user's membership record.

## Why This Design

The numeric-level approach avoids brittle string comparisons or hardcoded if/elif chains. Adding a new role (e.g., `BILLING` at level 1.5) only requires adding an enum member — all existing `check_*` calls automatically work correctly.

The `Forbidden` exception includes a machine-readable error code (like `workspace.insufficient_role`) so that API consumers can distinguish permission errors from other 403 scenarios without parsing human-readable messages.

## Known Gaps

- No `PocketAccess` guard is used in the workspace service — pocket-level checks likely live in a separate pocket service not included in this batch.
- The `from_str` methods do a linear scan over enum members. This is fine for 3-4 members but would need optimization if the enum grew significantly (unlikely for a role hierarchy).
