---
{
  "title": "User Model: Enterprise Users with OAuth and Multi-Workspace Membership",
  "summary": "Beanie document model for enterprise users, built on top of fastapi-users-db-beanie. Supports OAuth accounts (Google, GitHub), multi-workspace membership with per-workspace roles, presence status, and avatar profiles.",
  "concepts": [
    "User",
    "OAuthAccount",
    "WorkspaceMembership",
    "BeanieBaseUser",
    "fastapi-users",
    "OAuth",
    "presence",
    "multi-workspace"
  ],
  "categories": [
    "Models",
    "Authentication",
    "Data Layer",
    "Identity"
  ],
  "source_docs": [
    "78ba384603c02435"
  ],
  "backlinks": null,
  "word_count": 394,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# User Model: Enterprise Users with OAuth and Multi-Workspace Membership

## Purpose

The `User` model is the identity foundation of PocketPaw Enterprise. It extends `BeanieBaseUser` from the `fastapi-users-db-beanie` library, which provides built-in email/password authentication, verification, and account management. PocketPaw adds OAuth support, multi-workspace membership, and presence tracking on top.

## Design Decisions

### Extending fastapi-users
The `User` class inherits from both `BeanieBaseUser` and `Document` with a `# type: ignore[misc]` comment. The type-ignore is needed because the multiple inheritance confuses mypy — `BeanieBaseUser` already extends `Document` internally, creating a diamond inheritance. This is the recommended pattern from the fastapi-users documentation.

### OAuth Account Model
`OAuthAccount` extends `BaseOAuthAccount` with an empty body (`pass`). This exists as a customization hook — if PocketPaw needs to store additional OAuth metadata (e.g., token scopes, refresh timestamps), fields can be added here without modifying the base library class.

### Multi-Workspace Membership
The `WorkspaceMembership` embedded model tracks per-workspace roles. A user can belong to multiple workspaces with different roles in each (owner in one, viewer in another). The `active_workspace` field tracks which workspace the user is currently operating in, used to scope API requests.

### Presence Tracking
The `status` field (`online`/`offline`/`away`/`dnd`) and `last_seen` timestamp enable real-time presence features. The regex pattern constraint ensures only valid status values are stored.

### Email Collation
`email_collation = None` in Settings disables MongoDB's default collation for the users collection. This is likely set to avoid locale-specific case-sensitivity issues with email lookups — fastapi-users handles email normalization at the application level.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `full_name` | `str` | Display name |
| `avatar` | `str` | Avatar URL |
| `active_workspace` | `str | None` | Current workspace context |
| `workspaces` | `list[WorkspaceMembership]` | Multi-workspace roles |
| `status` | `str` | Presence: `online`, `offline`, `away`, `dnd` |
| `last_seen` | `datetime` | Last activity timestamp |
| `oauth_accounts` | `list[OAuthAccount]` | Linked OAuth providers |

## Known Gaps

- The `workspaces` list is embedded in the user document, which means workspace role changes require updating the user document. For large organizations with many workspace changes, this could cause write contention.
- No `WorkspaceMembership` deduplication — a bug could add the same workspace twice to the list.
- The `OAuthAccount` model is empty (just `pass`) but is still defined as a separate class, suggesting planned future extensions.
