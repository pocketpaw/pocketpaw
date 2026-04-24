---
{
  "title": "Workspace Invitation Model",
  "summary": "The Invite document manages workspace membership invitations sent to email addresses, tracking acceptance state, expiry, and optional group auto-assignment. It enforces a 7-day default TTL with timezone-aware expiry checking to prevent stale invitations from granting unintended access.",
  "concepts": [
    "workspace invitation",
    "Beanie Document",
    "token-based access",
    "role-based access control",
    "expiry",
    "timezone normalization",
    "indexed fields",
    "soft delete",
    "group auto-assignment",
    "Pydantic validation"
  ],
  "categories": [
    "data-models",
    "access-control",
    "enterprise-cloud"
  ],
  "source_docs": [
    "80e626a4825b25c6"
  ],
  "backlinks": null,
  "word_count": 577,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `Invite` model handles the lifecycle of workspace membership invitations in PocketPaw's enterprise cloud layer. An invitation is a time-limited credential that links an email address to a workspace, optionally pre-assigning a role and group membership.

## Why This Model Exists

Workspace access must be controlled. Rather than allowing arbitrary self-registration, new members are invited explicitly by existing members. The invite acts as an out-of-band authorization token: the sender specifies the role, the recipient proves identity by clicking a link containing the `token`, and the system validates the token has not expired or been revoked before granting membership.

## Key Fields and Their Purpose

**`token` (unique index)** — A unique string (typically a UUID or cryptographically random value generated at call-site) that serves as the bearer credential in invitation links. Indexing it for uniqueness ensures no two active invites share the same URL, preventing one invite from accidentally accepting another.

**`role` with regex pattern** — Constrained to `admin`, `member`, or `viewer` via Pydantic's `pattern` validator. This prevents invitation creation with arbitrary roles that the RBAC layer does not recognize, closing a class of privilege-escalation bugs at the document level before data ever reaches MongoDB.

**`expires_at` with `_default_expiry()`** — Set to UTC+7 days at creation time. The factory function is a module-level callable rather than a lambda so it can be unit-tested in isolation. The 7-day window balances usability (enough time for the recipient to act) against security (stale invites cannot be replayed indefinitely).

**`expired` property with timezone normalization** — The property defensively adds UTC tzinfo to naive datetimes before comparison. This guards against documents written by older code that stored naive datetimes, where a direct `datetime.now(UTC) > self.expires_at` comparison would raise a `TypeError`. This is a silent-failure prevention pattern: without it, an unhandled exception at the comparison site might cause the expiry check to be skipped entirely.

**`accepted` / `revoked` booleans** — Separate flags rather than a single status enum. This allows the service layer to distinguish between "used" (accepted) and "cancelled by admin" (revoked) without losing history. Both flags default to `False`; the service sets the appropriate flag on the corresponding action.

**`group` (optional)** — When an invite originates from a group invitation flow, this ID is stored so the accept handler can atomically add the new user to the group without a second lookup or race window.

## Indexed Fields

`workspace`, `email`, and `token` are wrapped with Beanie's `Indexed()` helper. The workspace and email indices support the common query "is there an open invite for this email in this workspace?" without a full collection scan. The token unique index enforces collision-free URL tokens at the database level as a second line of defense behind application-level generation.

## Failure Scenarios Prevented

- **Timezone mismatch crash**: naive `expires_at` from legacy writes would cause `TypeError` on comparison; the property normalizes before comparing.
- **Privilege escalation via invite**: the role pattern constraint prevents `role="superadmin"` or other unrecognized values from being persisted.
- **Replay of accepted invites**: the `accepted` flag lets the accept handler reject re-use without querying a separate audit log.

## Known Gaps

- No compound index on `(workspace, email, accepted, revoked)` — the common "list pending invites for a workspace" query will scan all invites in the workspace.
- The `token` value is generated at call-site (not in the model); the model provides no entropy guarantee.
- `expires_at` is not automatically enforced as a TTL index in MongoDB, so expired documents accumulate until explicitly purged.