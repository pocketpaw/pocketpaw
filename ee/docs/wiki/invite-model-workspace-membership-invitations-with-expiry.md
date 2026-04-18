---
{
  "title": "Invite Model: Workspace Membership Invitations with Expiry",
  "summary": "Beanie document model for workspace invitations sent via email. Supports role-based access, 7-day expiry, revocation, and optional auto-join to a specific group on acceptance.",
  "concepts": [
    "Invite",
    "workspace invitation",
    "token",
    "expiry",
    "timezone-aware datetime",
    "role-based access",
    "Beanie"
  ],
  "categories": [
    "Models",
    "Authentication",
    "Data Layer",
    "Workspace Management"
  ],
  "source_docs": [
    "80e626a4825b25c6"
  ],
  "backlinks": null,
  "word_count": 399,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Invite Model: Workspace Membership Invitations with Expiry

## Purpose

The `Invite` model tracks pending, accepted, and revoked workspace invitations. It enables workspace admins to invite users by email with a specific role, and optionally pre-assign them to a group upon acceptance.

## Design Decisions

### Unique Token Index
The `token` field is `Indexed(str, unique=True)`. This is critical for security — invite links contain the token, and the uniqueness constraint prevents token collisions. Without it, two invites could theoretically share a token, causing the wrong invite to be accepted.

### Timezone-Aware Expiry Check
The `expired` property contains a defensive pattern:
```python
if exp.tzinfo is None:
    exp = exp.replace(tzinfo=UTC)
```
This guards against MongoDB returning naive datetimes. When documents are loaded from MongoDB, datetime fields can lose their timezone info depending on the driver configuration. Without this guard, comparing a naive `expires_at` with a timezone-aware `datetime.now(UTC)` would raise a `TypeError`. This is a common pitfall with MongoDB + Python datetime handling.

### 7-Day Default Expiry
The `_default_expiry()` factory function creates expiry dates 7 days from now. This is a module-level function (not a lambda) because Pydantic's `default_factory` needs a callable, and using a named function makes the intent clearer than an inline lambda.

### Group Auto-Join
The optional `group` field allows invites to carry a group context. When an invite originated from a group share action, the accepting user should be auto-added to that group — reducing friction in the onboarding flow.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `workspace` | `Indexed(str)` | Target workspace |
| `email` | `Indexed(str)` | Invitee email |
| `role` | `str` | `admin`, `member`, or `viewer` |
| `invited_by` | `str` | User ID of inviter |
| `token` | `Indexed(str, unique=True)` | Secure invite token |
| `group` | `str | None` | Optional group to auto-join |
| `accepted` | `bool` | Whether invite was accepted |
| `revoked` | `bool` | Whether invite was revoked |
| `expires_at` | `datetime` | Expiry timestamp (default: 7 days) |

## Known Gaps

- No rate limiting on invite creation — a malicious admin could spam invites to arbitrary emails.
- The `accepted` and `revoked` fields are independent booleans, meaning both could theoretically be `True` simultaneously. A single `status` enum would be safer.
- No cascade behavior — if a workspace is deleted, orphaned invites remain in the collection.
