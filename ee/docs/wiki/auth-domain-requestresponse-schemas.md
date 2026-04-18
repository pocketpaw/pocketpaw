---
{
  "title": "Auth Domain Request/Response Schemas",
  "summary": "Pydantic schemas for the authentication domain, covering profile updates, workspace selection, and user response serialization. These schemas define the API contract between the frontend and the auth service layer.",
  "concepts": [
    "Pydantic",
    "BaseModel",
    "auth schemas",
    "ProfileUpdateRequest",
    "SetWorkspaceRequest",
    "UserResponse",
    "partial update",
    "from_attributes"
  ],
  "categories": [
    "authentication",
    "schemas",
    "API contracts"
  ],
  "source_docs": [
    "3f58e6ed5e03bcb5"
  ],
  "backlinks": null,
  "word_count": 263,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Auth Domain Request/Response Schemas

`cloud/auth/schemas.py`

## Purpose

This module defines the data transfer objects (DTOs) for the auth domain. These Pydantic models serve as the contract between HTTP request bodies and the `AuthService` business logic, ensuring type safety and validation at the API boundary.

## Models

### ProfileUpdateRequest

Allows partial updates to a user's profile. All fields are optional (`None` default), so the client only sends what changed. This prevents accidental field clearing — if a field is `None`, the service layer skips it rather than overwriting with null.

- `full_name: str | None`
- `avatar: str | None`
- `status: str | None`

### SetWorkspaceRequest

Sets the user's active workspace. The `workspace_id` is required (not optional) because switching to "no workspace" is not a valid operation.

### UserResponse

Serializes a `User` document for API responses. Notable:

- `model_config = {"from_attributes": True}` — enables constructing the response directly from ORM/document attributes using `.model_validate()`, avoiding manual dict construction.
- `workspaces: list[dict]` — uses a loose `dict` type rather than a strict schema, likely because workspace membership objects vary or are still evolving.

## Design Decisions

- **Partial update pattern**: Optional fields with `None` defaults is the standard PocketPaw pattern for PATCH-style updates.
- **No validation constraints**: Fields like `full_name` and `status` have no length limits. This relies on the service layer or database for enforcement.

## Known Gaps

- No length validation on `full_name`, `avatar`, or `status` fields — a very long string would pass schema validation.
- `workspaces` is typed as `list[dict]` rather than a proper typed model, which weakens type safety.
