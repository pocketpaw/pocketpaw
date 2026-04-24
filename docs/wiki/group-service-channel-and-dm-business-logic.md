---
{
  "title": "Group Service - Channel and DM Business Logic",
  "summary": "The `GroupService` class encapsulates all group and channel CRUD, membership management, agent attachment, and DM creation logic for the chat domain. It coordinates between the Group ODM model, the realtime event bus, and the RBAC/audit guard layer.",
  "concepts": [
    "GroupService",
    "RBAC",
    "group membership",
    "DM",
    "agent DM",
    "slug generation",
    "realtime events",
    "Beanie ODM",
    "idempotency",
    "role resolution",
    "CRUD",
    "audit"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "realtime",
    "RBAC"
  ],
  "source_docs": [
    "78b8f964762d7e30"
  ],
  "backlinks": null,
  "word_count": 439,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`GroupService` is the stateless business logic layer for the chat domain's group concept. A *group* can be a public channel, a private channel, a DM between two users, or a DM between a user and an AI agent. This single service handles all four variants, which share enough logic (membership checks, realtime event emission, slug generation) to warrant a unified implementation.

## Key Design Patterns

### Role Resolution and Guards

Every mutating method begins by resolving the caller's role and verifying permission through three internal guards:

- `_require_group_member(group, user_id)` - raises `Forbidden` if the user is not listed in the group's members.
- `_require_group_admin(group, user_id)` - raises `Forbidden` if the user is not an admin or owner.
- `_require_can_post(group, user_id)` - raises `Forbidden` if the user's role cannot write messages.

The public `resolve_group_role(group, user_id)` function provides structured role resolution consumed by the broader guards matrix that enforces RBAC across the platform.

### Realtime Event Emission

After every structural change (group created, member added, agent added), the service emits events onto `ee.cloud.realtime.bus`. This ensures connected WebSocket clients receive live updates without polling. Emit calls are fire-and-forget so they do not block the HTTP response path.

### Slug Generation

Slugs provide stable, URL-safe identifiers for groups. The regex normalisation (lowercase, spaces/underscores to hyphens, strip non-alnum) prevents duplicate slugs caused by capitalisation or punctuation differences.

### DM Idempotency

Both `get_or_create_dm` and `get_or_create_agent_dm` use find-or-create semantics. If the DM already exists, the same document is returned. This prevents a race condition where two rapid calls from different tabs both conclude the DM does not exist and insert duplicate records.

## Method Highlights

- **`create_group`** - validates name uniqueness within the workspace, generates a slug, inserts the Group document, then emits `group.created`.
- **`add_members` / `remove_member`** - idempotent membership mutations that skip no-ops silently, preventing errors from double-clicks.
- **`set_member_role`** - returns the new `MemberRole` so the router can echo it back without a second fetch.
- **`add_agent` / `update_agent` / `remove_agent`** - attach AI agents to groups with configurable `respond_mode` (auto, mention-only, etc.).
- **`list_member_ids`** - lightweight helper returning only IDs, used by the realtime layer to know which sockets to notify without loading full member documents.

## Response Shaping

`_group_response(group)` converts the Beanie document into a frontend-compatible dict. Centralising this here means the HTTP response shape and the realtime push shape are identical - the client uses the same handler regardless of data arrival path.

## Known Gaps

- Audit logging coverage is incomplete - not all mutating operations emit audit events.
- `_generate_slug` does not guarantee uniqueness across concurrent inserts; a unique database index is the final safety net.