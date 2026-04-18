---
{
  "title": "FastAPI Dependencies for Cloud Authentication and Authorization",
  "summary": "Provides reusable FastAPI dependency functions that extract the authenticated user, user ID, and workspace ID from JWT tokens. Also includes a require_role dependency factory for role-based access control on workspace operations.",
  "concepts": [
    "FastAPI dependencies",
    "JWT authentication",
    "workspace context",
    "role-based access control",
    "dependency factory",
    "authorization"
  ],
  "categories": [
    "cloud",
    "shared",
    "authentication",
    "authorization",
    "FastAPI"
  ],
  "source_docs": [
    "abd1ca07db31e359"
  ],
  "backlinks": null,
  "word_count": 252,
  "compiled_at": "2026-04-08T07:25:31Z",
  "compiled_with": "agent",
  "version": 1
}
---

# FastAPI Dependencies for Cloud Authentication and Authorization

## Purpose

This module defines the FastAPI `Depends()` functions used across all cloud routers to extract identity and authorization context from incoming requests. Every authenticated endpoint uses at least one of these dependencies.

## Dependencies

### Identity Extraction

- **`current_user()`** — returns the full `User` document from the JWT token via `current_active_user`
- **`current_user_id()`** — returns just the string user ID (most common dependency since services need IDs, not full documents)
- **`current_workspace_id()`** — extracts the user's `active_workspace` field; raises HTTP 400 if no workspace is set

### Optional Workspace

`optional_workspace_id()` returns the workspace ID or `None`. Used by endpoints that can work without a workspace context (e.g., workspace creation itself).

### Role-Based Access Control

`require_role(minimum)` is a dependency factory that returns a new dependency function. When used:

1. Fetches the authenticated user
2. Resolves the current workspace ID
3. Finds the user's membership record for that workspace
4. Calls `check_workspace_role()` to verify the role meets the minimum requirement
5. Raises `Forbidden` if the user isn't a workspace member

Usage: `Depends(require_role("admin"))` on any endpoint that needs elevated permissions.

## Error Handling

- Missing workspace raises `HTTPException(400)` with a descriptive message
- Missing workspace membership raises `Forbidden("workspace.not_member")`
- Role insufficiency is handled by `check_workspace_role()` (in the permissions module)

## Design Notes

These dependencies form a layered chain: `current_active_user` (from auth module) → `current_user` → `current_user_id` / `current_workspace_id`. FastAPI's dependency injection caches results per-request, so `current_active_user` is only called once even if multiple dependencies use it.
