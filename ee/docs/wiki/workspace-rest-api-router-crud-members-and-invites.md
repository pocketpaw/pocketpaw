---
{
  "title": "Workspace REST API Router: CRUD, Members, and Invites",
  "summary": "FastAPI router defining all workspace HTTP endpoints — workspace CRUD, member management, and invite lifecycle. All routes require a valid license (via require_license dependency) and authenticated user (via current_user), with the single exception of invite validation which is public.",
  "concepts": [
    "APIRouter",
    "workspace CRUD",
    "member management",
    "invite lifecycle",
    "require_license",
    "current_user",
    "FastAPI dependencies"
  ],
  "categories": [
    "cloud",
    "workspace",
    "API",
    "REST endpoints"
  ],
  "source_docs": [
    "5c6a136762611a1a"
  ],
  "backlinks": null,
  "word_count": 343,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Workspace REST API Router: CRUD, Members, and Invites

## Purpose

This router is the HTTP interface for the workspace domain. It translates HTTP requests into calls to `WorkspaceService`, keeping the router thin — no business logic lives here.

## Route Groups

### Workspace CRUD (`/workspaces`)

| Method | Path | Handler | Auth |
|--------|------|---------|------|
| POST | `/workspaces` | `create_workspace` | User |
| GET | `/workspaces` | `list_workspaces` | User |
| GET | `/workspaces/{id}` | `get_workspace` | User |
| PATCH | `/workspaces/{id}` | `update_workspace` | User |
| DELETE | `/workspaces/{id}` | `delete_workspace` | User |

DELETE returns 204 with an empty `Response` body — FastAPI requires explicit `Response(status_code=204)` to avoid serialization issues with `None`.

### Members (`/workspaces/{id}/members`)

| Method | Path | Handler |
|--------|------|--------|
| GET | `/{id}/members` | `list_members` |
| PATCH | `/{id}/members/{user_id}` | `update_member_role` |
| DELETE | `/{id}/members/{user_id}` | `remove_member` |

### Invites (`/workspaces/{id}/invites`)

| Method | Path | Handler | Auth |
|--------|------|---------|------|
| POST | `/{id}/invites` | `create_invite` | User |
| GET | `/invites/{token}` | `validate_invite` | **None** |
| POST | `/invites/{token}/accept` | `accept_invite` | User |
| DELETE | `/{id}/invites/{invite_id}` | `revoke_invite` | User |

The `validate_invite` endpoint is deliberately unauthenticated — it allows invite recipients to preview invite details before signing in or creating an account.

## Global Dependencies

The router applies `Depends(require_license)` at the router level, meaning every endpoint requires a valid enterprise license. Individual endpoints add `Depends(current_user)` for authentication.

## Design Decisions

- **Thin router pattern**: All logic delegates to `WorkspaceService` static methods. The router only handles HTTP concerns (status codes, response format).
- **Schema validation**: Request bodies use Pydantic models from `schemas.py`, giving automatic 422 responses for malformed input.
- **Return dicts, not models**: Service methods return plain dicts rather than Pydantic response models, giving the service layer full control over the shape of API responses.

## Known Gaps

- No pagination on `list_workspaces` or `list_members` — could become a problem for large workspaces.
- No rate limiting on invite creation or acceptance.
