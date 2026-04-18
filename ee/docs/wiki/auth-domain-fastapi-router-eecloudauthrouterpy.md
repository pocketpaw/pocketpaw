---
{
  "title": "Auth Domain FastAPI Router (ee/cloud/auth/router.py)",
  "summary": "The HTTP routing layer for authentication in PocketPaw Enterprise Cloud. Mounts fastapi-users' built-in auth routes for both cookie and bearer backends, adds registration, and provides profile management and workspace switching endpoints.",
  "concepts": [
    "auth router",
    "fastapi-users routes",
    "cookie login",
    "bearer login",
    "profile management",
    "workspace switching"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "authentication",
    "API"
  ],
  "source_docs": [
    "be3c6afb082643e0"
  ],
  "backlinks": null,
  "word_count": 301,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Auth Domain FastAPI Router

## Purpose

This router assembles all authentication-related HTTP endpoints. It follows PocketPaw's thin-router pattern — most logic lives in `AuthService` and `fastapi_users`, with the router handling only request parsing and response formatting.

## Route Structure

### fastapi-users Built-in Routes

Three sets of auto-generated routes from the `fastapi-users` library:

| Prefix | Backend | Endpoints |
|--------|---------|----------|
| `/auth` | Cookie | `POST /login`, `POST /logout` |
| `/auth/bearer` | Bearer | `POST /login`, `POST /logout` |
| `/auth` | Register | `POST /register` |

The cookie and bearer backends share the same auth logic but return tokens differently — cookie backend sets an HTTP cookie, bearer backend returns the token in the response body.

### Profile Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/auth/me` | Get current user profile |
| PATCH | `/auth/me` | Update profile fields |
| POST | `/auth/set-active-workspace` | Switch active workspace context |

All profile endpoints require `current_active_user` — they reject unauthenticated and inactive users.

### Workspace Switching

`set-active-workspace` accepts a `SetWorkspaceRequest` with a `workspace_id` and updates the user's active workspace. This is critical for multi-tenant operation — the active workspace determines which agents, pockets, and chat sessions are visible.

## Service Delegation

Profile operations delegate to `AuthService` (in `service.py`), keeping the router thin. The workspace switch returns a confirmation dict directly since the operation is simple.

## Known Gaps

- No password change or password reset endpoints are mounted. `fastapi-users` provides these (`get_reset_password_router`, `get_verify_router`) but they aren't included here.
- No OAuth/social login routes are mounted despite the `OAuthAccount` model existing in the user model.
- The `set-active-workspace` endpoint doesn't validate that the user is a member of the target workspace — validation presumably happens in `AuthService`, but it's not visible from this file.