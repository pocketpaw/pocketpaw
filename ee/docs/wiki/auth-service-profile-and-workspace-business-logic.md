---
{
  "title": "Auth Service — Profile and Workspace Business Logic",
  "summary": "Stateless service class encapsulating authentication-related business logic: profile retrieval, profile updates, and workspace switching. Follows the PocketPaw pattern of static async methods on a service class.",
  "concepts": [
    "AuthService",
    "stateless service",
    "partial update",
    "get_profile",
    "update_profile",
    "set_active_workspace",
    "User model"
  ],
  "categories": [
    "authentication",
    "business logic",
    "services"
  ],
  "source_docs": [
    "e6b851bb0e684a5b"
  ],
  "backlinks": null,
  "word_count": 304,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Auth Service — Profile and Workspace Business Logic

`cloud/auth/service.py`

## Purpose

`AuthService` is the business logic layer for auth operations. It sits between the router (HTTP layer) and the `User` document model (database layer), enforcing rules and transforming data. The class is stateless — all methods are `@staticmethod` — which means no instance state to manage and easy testing.

## Methods

### get_profile(user: User) -> dict

Converts a `User` document into a frontend-friendly dictionary. Key transformations:

- `user.full_name` -> `"name"` (field rename for frontend convention)
- `user.avatar` -> `"image"` (matches frontend component expectations)
- `user.is_verified` -> `"emailVerified"` (camelCase for JS clients)
- Workspace list is flattened to `[{workspace, role}]` dicts

This avoids exposing internal field names or document structure to the API.

### update_profile(user: User, body: ProfileUpdateRequest) -> dict

Implements partial updates using the `if field is not None` guard pattern. This prevents a client from accidentally clearing fields by sending `null`. After saving, it returns the full updated profile by calling `get_profile` — this ensures the response always reflects the persisted state.

### set_active_workspace(user: User, workspace_id: str) -> None

Switches the user's active workspace. Includes explicit validation for empty `workspace_id` with a 400 HTTP error. This defensive check exists because an empty string would technically pass type validation but is semantically invalid.

## Design Patterns

- **Stateless service**: No `__init__`, no instance variables. All state comes from parameters. This pattern is used throughout PocketPaw's cloud domain.
- **Partial update guard**: `if body.field is not None` prevents accidental overwrites.
- **Return after save**: `update_profile` re-reads via `get_profile` to return consistent data.

## Known Gaps

- `set_active_workspace` does not verify that the user actually belongs to the given workspace. A user could set any workspace ID.
- No authorization check on `update_profile` — it assumes the router already verified the user owns the profile.
