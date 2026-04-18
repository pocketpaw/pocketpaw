---
{
  "title": "Workspace Service: Business Logic for CRUD, Members, and Invites",
  "summary": "Stateless service class containing all workspace business logic — workspace lifecycle, member role management, and invite flow with seat-limit enforcement. Uses Beanie ODM for MongoDB operations and emits domain events via an event bus for cross-cutting concerns like notifications.",
  "concepts": [
    "WorkspaceService",
    "Beanie ODM",
    "soft delete",
    "seat limit",
    "invite token",
    "event_bus",
    "member management",
    "ownership protection",
    "idempotency guard"
  ],
  "categories": [
    "cloud",
    "workspace",
    "business logic",
    "authorization",
    "invites"
  ],
  "source_docs": [
    "32352af182abe167"
  ],
  "backlinks": null,
  "word_count": 619,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Workspace Service: Business Logic for CRUD, Members, and Invites

## Purpose

`WorkspaceService` is the core business logic layer for the workspace domain. It sits between the thin HTTP router and the database models, enforcing authorization rules, business constraints (seat limits, ownership protection), and domain events.

## Architecture

All methods are `@staticmethod` — the service is intentionally stateless with no instance variables. This makes it easy to test (no setup/teardown) and signals that all state lives in the database.

### Helper Functions

- `_workspace_response(ws, member_count)` — Converts a Beanie `Workspace` document into a frontend-compatible dict with camelCase keys (`createdAt`, `memberCount`). This mapping exists because the Python models use snake_case but the frontend expects camelCase.
- `_invite_response(invite)` — Same pattern for Invite documents.
- `_get_membership(user, workspace_id)` — Scans the user's `workspaces` list for matching membership. Raises `NotFound` if absent, which doubles as an authorization check (you can't operate on a workspace you're not a member of).
- `_count_members(workspace_id)` — MongoDB aggregation count of users with matching workspace membership.

## Workspace CRUD

### Create

1. Checks slug uniqueness among non-deleted workspaces (soft-delete aware via `deleted_at == None`)
2. Creates workspace document
3. Adds creator as `owner` member in their user document
4. Sets as active workspace

The `# noqa: E711` comment on `deleted_at == None` is necessary because Beanie requires `== None` for MongoDB null checks (Python's `is None` doesn't translate to MongoDB query syntax).

### Get / List

Both require membership (via `_get_membership`). `list_for_user` fetches member counts per workspace, which means N+1 queries — one count query per workspace.

### Update

Requires `admin+` role. Only applies non-None fields from the request body (partial update pattern).

### Delete

Soft-delete only (sets `deleted_at` timestamp). Requires `owner` role. Does not remove member references — members will see the workspace disappear from lists because queries filter on `deleted_at: None`.

## Member Management

### Update Role

- Requires `admin+`
- **Owner protection**: Cannot demote the workspace owner — this prevents accidental lockout where no one has owner privileges.
- Loads both the requesting user and target user, verifying membership for both.

### Remove Member

- Requires `admin+`
- **Owner protection**: Cannot remove workspace owner.
- Clears `active_workspace` if the removed workspace was the user's active one.
- Emits `member.removed` event for downstream consumers (notifications, analytics).

## Invite Flow

### Create Invite

1. Requires `admin+`
2. Checks seat limit (`member_count >= ws.seats` prevents over-enrollment)
3. Checks for existing pending invites — but scoped by group: different groups can each have pending invites for the same email, while workspace-level invites (no group) are deduplicated.
4. Generates a `secrets.token_urlsafe(32)` token for the invite link.

### Accept Invite

1. Validates invite state: not accepted, not revoked, not expired
2. Checks seat limit again (seats may have filled between invite creation and acceptance)
3. Skips membership addition if user is already a member (idempotency guard — prevents duplicate membership entries if someone clicks "accept" twice)
4. Emits `invite.accepted` event with group context

### Validate Invite

Public endpoint (no auth) — allows previewing invite details before account creation.

### Revoke Invite

Requires `admin+`. Sets `revoked = True` flag.

## Known Gaps

- **N+1 query in `list_for_user`**: Fetches member count per workspace individually. Should use aggregation pipeline for workspaces with many members.
- **No transaction boundaries**: Workspace creation writes to both Workspace and User collections without a transaction. If the user save fails after workspace insert, you get an orphaned workspace.
- **Soft-delete cleanup**: Deleted workspaces are never hard-deleted or cleaned up. Member references to deleted workspaces persist in user documents.
- **No invite expiration enforcement**: The `expired` property is checked on accept but there's no background job to mark invites as expired — the model likely computes `expired` from `expires_at` dynamically.
