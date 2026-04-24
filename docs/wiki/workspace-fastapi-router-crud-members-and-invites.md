---
{
  "title": "Workspace FastAPI Router — CRUD, Members, and Invites",
  "summary": "This module defines all HTTP endpoints for the workspace domain — workspace lifecycle, member management, and invite flows — using FastAPI's dependency injection to enforce authorization at the route layer while keeping service methods auth-agnostic. Authorization gates are declared inline as `Depends(require_action(...))` so the permission model is readable directly from the route signature.",
  "concepts": [
    "FastAPI router",
    "dependency injection",
    "require_action",
    "require_membership",
    "current_user",
    "invite token",
    "authorization",
    "workspace CRUD",
    "member management",
    "license gate"
  ],
  "categories": [
    "workspace",
    "authorization",
    "HTTP API",
    "FastAPI",
    "invite flow"
  ],
  "source_docs": [
    "d302790988aecc7a"
  ],
  "backlinks": null,
  "word_count": 406,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The workspace router is structured around three groups of endpoints: workspace CRUD, member management, and invite management. All routes are mounted under `/workspaces` and require a valid license (via the `require_license` dependency applied at the router level). This means every endpoint in this module fails fast with a 402 or 403 before any service code runs if the license check fails.

## Authorization Architecture

The router uses a layered authorization model that separates authentication from permission checks:

- `current_user` — injects the authenticated user; fails with 401 if no session.
- `require_membership` — confirms the user belongs to the requested workspace; fails with 403 if not.
- `require_action("workspace.update")` — checks the user's role permits the named action; fails with 403 if denied.

Service methods receive an already-vetted `User` object and do not re-check roles. This split prevents the common mistake of partially duplicating access logic in both the HTTP layer and the service layer, which can create gaps when one layer is updated but not the other.

## Workspace CRUD

The `POST /workspaces` endpoint has a deliberate gap in role checking: when a workspace does not yet exist there is no membership to check, so any authenticated user may create one. The comment "No workspace yet → no role check possible" makes this intent explicit. Subsequent operations (`PATCH`, `DELETE`) require `workspace.update` and `workspace.delete` actions respectively.

## Member Management

Listing members requires only membership, while role changes and removals require elevated actions. This is intentional — a basic member should be able to see who else is in the workspace without having administrative powers.

## Invite Flow

The invite endpoints illustrate a key design decision: `GET /invites/{token}` and `POST /invites/{token}/accept` do not use `require_membership`. The invite token is the authorization artifact for validation and acceptance. A user must be authenticated to accept (via `current_user`), but they cannot be a member yet — that is the whole point of the flow. The comment "the invite token itself is the authorization artifact" documents why the seemingly missing role check is correct behavior, not an oversight.

```python
@router.post("/invites/{token}/accept")
async def accept_invite(
    token: str,
    user: User = Depends(current_user),
) -> dict:
    # Accepting an invite requires only authentication;
    # the invite token itself is the authorization artifact.
    await WorkspaceService.accept_invite(token, user)
    return {"ok": True}
```

## Known Gaps

No known gaps identified in the router itself. The service layer handles idempotency and data invariants (e.g., seat limit checks, owner-demotion guards).