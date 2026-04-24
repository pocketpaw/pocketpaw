---
{
  "title": "Auth Service - User Profile and Workspace Management",
  "summary": "A stateless FastAPI service class handling the business logic for user authentication, profile management, and active workspace switching. It keeps route handlers thin by acting as the bridge between HTTP requests and the User ODM model.",
  "concepts": [
    "AuthService",
    "user profile",
    "workspace switching",
    "stateless service",
    "Beanie ODM",
    "FastAPI",
    "HTTPException",
    "profile update",
    "partial update",
    "ObjectId"
  ],
  "categories": [
    "auth",
    "user management",
    "cloud EE",
    "FastAPI"
  ],
  "source_docs": [
    "f18a27189dc7803f"
  ],
  "backlinks": null,
  "word_count": 372,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`AuthService` is a stateless class that centralises auth domain business logic. Because it carries no instance state, every method is a `@staticmethod`, making it safe to call from any coroutine without shared mutable state concerns.

## Why a Dedicated Service Layer?

FastAPI route handlers tend to accumulate logic over time. `AuthService` separates *what to do* (business logic) from *how to receive the request* (router). This also makes the logic independently testable: tests can call `AuthService.get_profile(mock_user)` directly without standing up an HTTP server.

## Methods

### `get_profile(user)`

Returns a serialised snapshot of the caller's account including workspace membership with role metadata (`[{"workspace": ..., "role": ...}]`), which the frontend uses to build the workspace switcher. The `emailVerified` field signals whether the user needs to confirm their email address.

The explicit `str(user.id)` cast is defensive: MongoDB ObjectIds are not JSON-serialisable by default, so omitting it would cause a 500 at the serialisation boundary.

### `update_profile(user, body)`

Applies partial updates - only fields that are not `None` in `ProfileUpdateRequest` are written. This prevents overwriting existing values when the client sends only a subset of fields. After saving, it calls `get_profile` to return the canonical shape, ensuring the response always reflects the current persisted state.

### `set_active_workspace(user, workspace_id)`

Persists the user's currently selected workspace. It raises `HTTPException(400)` early if `workspace_id` is falsy - this guards against clients accidentally passing an empty string and silently erasing the user's active workspace. There is intentionally **no membership check** here; the route handler or a guard is expected to validate workspace membership before reaching the service.

## Design Decisions

- **Stateless statics**: stateless methods are easier to test, thread-safe, and require no DI wiring.
- **Thin response shaping in the service**: `get_profile` builds the response dict rather than returning the ORM object directly, giving the service full control over the API contract without coupling it to Pydantic response models.
- **`await user.save()`**: uses Beanie's async save, consistent with the async FastAPI runtime.

## Known Gaps

- `set_active_workspace` does not verify that the user is a member of the target workspace. Callers rely on upstream guards to enforce this invariant.
- No audit logging on profile updates - there is no record of when a display name or avatar changed.