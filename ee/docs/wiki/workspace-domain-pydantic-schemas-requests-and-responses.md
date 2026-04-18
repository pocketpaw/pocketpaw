---
{
  "title": "Workspace Domain Pydantic Schemas: Requests and Responses",
  "summary": "Defines Pydantic models for all workspace API request and response payloads. Includes input validation such as slug format enforcement and role value constraints via regex patterns.",
  "concepts": [
    "Pydantic schemas",
    "field_validator",
    "slug validation",
    "request validation",
    "CreateWorkspaceRequest",
    "CreateInviteRequest",
    "WorkspaceResponse"
  ],
  "categories": [
    "cloud",
    "workspace",
    "validation",
    "API schemas"
  ],
  "source_docs": [
    "954cf1c082222e56"
  ],
  "backlinks": null,
  "word_count": 234,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Workspace Domain Pydantic Schemas: Requests and Responses

## Purpose

These schemas serve two roles: they validate incoming request data (rejecting malformed input with 422 errors before it reaches business logic) and they define the shape of API responses for documentation and client generation.

## Request Schemas

### CreateWorkspaceRequest

- `name`: 1-100 characters
- `slug`: 1-50 characters, validated by `validate_slug` to enforce lowercase alphanumeric with hyphens, must start and end with alphanumeric. This prevents slugs like `--bad-` or `UPPER` from being stored, which would break URL routing and cross-workspace lookups.

### UpdateWorkspaceRequest

- `name` and `settings` are both optional (`None` default), allowing partial updates via PATCH semantics.

### CreateInviteRequest

- `email`: string (no format validation beyond Pydantic's type check)
- `role`: constrained to `admin` or `member` via regex pattern — notably excludes `owner`, preventing invite-based ownership transfer.
- `group_id`: optional, for group-scoped invites.

### UpdateMemberRoleRequest

- `role`: constrained to `owner|admin|member` — allows all three roles unlike invite creation. Ownership transfer is handled through role updates, not invites.

## Response Schemas

- `WorkspaceResponse`: includes computed `member_count` field (default 0)
- `MemberResponse`: includes `joined_at` timestamp
- `InviteResponse`: includes status flags (`accepted`, `revoked`, `expired`) and expiration timestamp

## Known Gaps

- `CreateInviteRequest.email` has no email format validation — any string is accepted.
- No `WorkspaceResponse` includes `settings` — workspace settings are accepted on update but not returned in responses (they may be returned in a different shape).
