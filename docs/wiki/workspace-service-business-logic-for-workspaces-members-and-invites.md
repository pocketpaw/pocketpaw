---
{
  "title": "Workspace Service — Business Logic for Workspaces, Members, and Invites",
  "summary": "The `WorkspaceService` class is a stateless async service that encapsulates all business logic for the workspace domain, including workspace lifecycle, member management, invite flow, and realtime event emission. It enforces several data-level invariants (slug uniqueness, seat limits, owner demotion guard) that cannot be expressed as route-level permission checks.",
  "concepts": [
    "WorkspaceService",
    "soft delete",
    "slug uniqueness",
    "seat limit",
    "invite flow",
    "owner invariant",
    "realtime events",
    "resolver invalidation",
    "N+1 query",
    "CSPRNG token",
    "Beanie",
    "stateless service"
  ],
  "categories": [
    "workspace",
    "business logic",
    "service layer",
    "invite flow",
    "realtime"
  ],
  "source_docs": [
    "c280da6a7237b37e"
  ],
  "backlinks": null,
  "word_count": 546,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`WorkspaceService` is a collection of `@staticmethod` async methods — stateless by design so the router can call them without managing a service instance. This pattern is common in FastAPI applications where dependency injection is used for infrastructure (DB sessions, caches) but the service logic itself has no mutable state.

## Workspace Lifecycle

**Create** checks slug uniqueness before inserting — slugs are used in URLs and must be globally unique. The `# noqa: E711` on the `deleted_at == None` comparison is a known quirk of Beanie/MongoDB queries where `is None` does not produce the right filter operator.

**Delete** is a soft delete: `deleted_at` is stamped rather than the document being removed. The rationale is documented inline: downstream artefacts (pockets, agents, sessions) remain in the DB but orphan naturally because API endpoints already scope reads by the caller's active workspace. A future sweeper can purge them physically. The delete also cascades membership: every user who was a member has the workspace stripped from their `workspaces` list, and users whose `active_workspace` was the deleted one get it swapped to another membership or reset to `None` so the first-run workspace modal fires again.

## Owner Invariants

Two data-level invariants are enforced in the service, not the router, because they are data rules rather than role rules:

1. The workspace owner cannot be demoted via `update_member_role` — the service checks `ws.owner == target_user_id` and raises `Forbidden` if a non-owner role is requested.
2. The workspace owner cannot be removed via `remove_member` — same check, same exception.

These guards live in the service because the router's `require_action` dependency only knows about the caller's role, not about the target user's relationship to the workspace.

## Invite Flow

The invite lifecycle is carefully guarded:

- **Seat limit** is checked at invite creation and again at acceptance. Checking at creation prevents sending invites you cannot honor; checking at acceptance handles the race window where the workspace filled up between invite send and acceptance.
- **Deduplication** prevents sending duplicate pending invites to the same email, scoped per group so a workspace-level and a group-level invite can coexist.
- **Tokens** are generated with `secrets.token_urlsafe(32)` — 32 bytes of CSPRNG output, not guessable.
- **Notification** is created for existing users so their bell icon lights up immediately without requiring them to check email.

## Realtime Events

Every mutating operation emits one or more realtime events (via `emit(...)`) and invalidates the workspace resolver cache (`get_resolver().invalidate_workspace(...)`). The resolver invalidation ensures that presence fan-out and audience lookups immediately reflect the new membership state rather than serving stale data from a previous cache fill.

## Audience Helpers

The `list_member_ids`, `list_admin_ids`, and `list_peer_ids` methods exist solely for the realtime bus's audience resolver. They are not part of the CRUD surface but are needed to compute who should receive which events. `list_peer_ids` has a try/except around the ObjectId parse to handle the case where the user ID is malformed — this prevents a 500 error if the realtime system passes an invalid ID.

## Known Gaps

The `list_for_user` method calls `_count_members` in a loop, which issues one MongoDB query per workspace. At small team sizes this is fine; at scale (workspaces with hundreds of members or users with dozens of workspaces) this becomes an N+1 query pattern that warrants a `$facet` aggregation.