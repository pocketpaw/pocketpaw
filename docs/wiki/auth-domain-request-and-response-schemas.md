---
{
  "title": "Auth Domain Request and Response Schemas",
  "summary": "The auth schemas module defines the three Pydantic models used by the auth router: a profile update body, a workspace-switch body, and a user response envelope that shapes how user records are serialised for API consumers.",
  "concepts": [
    "Pydantic",
    "ProfileUpdateRequest",
    "SetWorkspaceRequest",
    "UserResponse",
    "from_attributes",
    "request schema",
    "response schema",
    "auth domain"
  ],
  "categories": [
    "auth",
    "validation",
    "API"
  ],
  "source_docs": [
    "3f58e6ed5e03bcb5"
  ],
  "backlinks": null,
  "word_count": 325,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Schema Inventory

### `ProfileUpdateRequest`

Used by `PATCH /auth/me`. All three fields are optional so the caller can update any subset of the profile without resending unchanged data:

```python
class ProfileUpdateRequest(BaseModel):
    full_name: str | None = None
    avatar: str | None = None
    status: str | None = None
```

The `avatar` field here accepts a URL string, not a file — avatar file uploads use a separate multipart endpoint. This two-path design keeps the profile update route simple (JSON body only) while the avatar upload route handles the complexity of file handling and storage.

### `SetWorkspaceRequest`

Used by `POST /auth/set-active-workspace`. A single-field schema:

```python
class SetWorkspaceRequest(BaseModel):
    workspace_id: str
```

The minimal surface is intentional — workspace switching is a simple state update that does not need additional context.

### `UserResponse`

The canonical user response envelope. Maps database field names to the shape expected by the frontend:

```python
class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    image: str
    email_verified: bool
    active_workspace: str | None
    workspaces: list[dict]
    model_config = {"from_attributes": True}
```

`from_attributes = True` enables construction from ORM/ODM objects directly (`UserResponse.model_validate(user)`) without manually extracting fields. The `workspaces: list[dict]` type is untyped — workspace membership records are passed through as raw dicts rather than strongly-typed Pydantic models.

## Why Separate Schemas from the Auth Core

Keeping schemas in their own file serves two purposes. First, it allows `auth/router.py` and `auth/service.py` to import schemas without importing the heavier `auth/core.py` (which pulls in fastapi-users, beanie, and the auth backends). Second, it makes the public API contract of the domain legible at a glance — reading `schemas.py` shows exactly what the domain accepts and returns.

## Known Gaps

- `UserResponse.workspaces` is typed as `list[dict]` rather than a typed Pydantic model for workspace membership. This means the response shape for workspace data is not validated or documented by the schema.
- `status` in `ProfileUpdateRequest` has no validation on allowed values — any string can be stored as a user status.