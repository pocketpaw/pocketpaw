---
{
  "title": "User and OAuth Account Models",
  "summary": "The `User` document extends `fastapi-users`' Beanie base to add enterprise-specific fields: multi-workspace membership with per-workspace roles, presence status, OAuth provider linkage, and an active workspace pointer. The `WorkspaceMembership` embedded model tracks role and join timestamp per workspace.",
  "concepts": [
    "User document",
    "BeanieBaseUser",
    "fastapi-users",
    "OAuth account",
    "WorkspaceMembership",
    "presence status",
    "multi-workspace",
    "role-based access",
    "email collation"
  ],
  "categories": [
    "data-models",
    "authentication",
    "enterprise-cloud"
  ],
  "source_docs": [
    "4aa74a9dd67b49d0"
  ],
  "backlinks": null,
  "word_count": 544,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's EE tier uses `fastapi-users` for authentication infrastructure (JWT/cookie auth, password hashing, email verification). The `User` model extends `BeanieBaseUser` to add enterprise features that `fastapi-users` does not provide out of the box: multi-workspace membership, presence, and OAuth account linking.

## `BeanieBaseUser` Extension

`BeanieBaseUser` provides `email`, `hashed_password`, `is_active`, `is_superuser`, and `is_verified`. The `User` class adds:

**`full_name` and `avatar`** — Display fields for the workspace UI. Both default to empty string so they are optional on creation; users without a display name fall back to their email address in the frontend.

**`active_workspace: str | None`** — A pointer to the workspace the user is currently operating in. The frontend uses this to determine which workspace context to load on login without requiring the user to re-select a workspace on every session. It is nullable because a new user may not have joined any workspace yet.

**`workspaces: list[WorkspaceMembership]`** — The embedded membership list. Each entry records the workspace ID, the user's role in that workspace, and the join timestamp. Embedding rather than using a join table means loading a user loads their complete membership map in one read — the dominant pattern when checking authorization ("does this user have `admin` role in workspace X?").

## `WorkspaceMembership` Design

The role field defaults to `"member"` and accepts `owner`, `admin`, `member`, or `viewer` as documented values. Unlike `Invite.role`, there is no Pydantic pattern constraint here — the role string is not validated at the model level. This flexibility allows the invite-to-membership promotion flow to pass through roles without a model-level regex conflict, but it also means invalid roles can be stored if the service layer does not validate.

The `joined_at` timestamp defaults to `datetime.now(UTC)` via a lambda factory. It records when membership was granted, which is useful for audit logs and for sorting members in the workspace member list by seniority.

## `OAuthAccount`

`OAuthAccount` extends `BaseOAuthAccount` from `fastapi_users_db_beanie` with no additional fields. The pass-through class exists so the `User.oauth_accounts` list has a concrete Beanie-aware type that can be embedded and deserialized correctly. `BaseOAuthAccount` itself stores the OAuth provider name, account ID, access token, refresh token, and expiry.

## Presence Status

`status` is constrained to `online`, `offline`, `away`, or `dnd` via a Pydantic pattern. This is the four-state presence model common in team chat applications. Presence is updated by the WebSocket connection manager when users connect/disconnect and is read by the group chat sidebar to show availability indicators.

`last_seen` defaults to `datetime.now(UTC)` and is updated on every API request (or on WebSocket disconnect). It powers the "last seen X minutes ago" display for users in `offline` state.

## `email_collation = None`

The `Settings` class sets `email_collation = None`, disabling `fastapi-users`' default case-insensitive email collation. This is a deliberate choice — or potentially a known gap — since disabling collation means `User@example.com` and `user@example.com` would be treated as distinct emails. Teams relying on case-insensitive email matching should verify this setting.

## Known Gaps

- `WorkspaceMembership.role` has no Pydantic pattern constraint, unlike `Invite.role` — invalid roles can be persisted.
- `email_collation = None` disables case-insensitive email matching; this may cause duplicate-email issues with providers that normalise email case differently.
- No index on `workspaces.workspace` — finding all members of a workspace requires a MongoDB `$elemMatch` scan across the entire `users` collection.