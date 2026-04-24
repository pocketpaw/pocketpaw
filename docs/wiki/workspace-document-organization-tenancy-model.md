---
{
  "title": "Workspace Document: Organization Tenancy Model",
  "summary": "The `Workspace` document is the top-level tenancy boundary in PocketPaw EE, representing one organization or deployment. It records billing plan, seat count, configurable settings, and supports soft deletion, with a unique `slug` index for human-readable URL routing.",
  "concepts": [
    "Workspace document",
    "multi-tenancy",
    "slug",
    "plan",
    "seats",
    "WorkspaceSettings",
    "soft delete",
    "retention policy",
    "default agent",
    "tenancy boundary"
  ],
  "categories": [
    "data-models",
    "tenancy",
    "enterprise-cloud"
  ],
  "source_docs": [
    "8cf39bd5bcaf7029"
  ],
  "backlinks": null,
  "word_count": 516,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

In PocketPaw's enterprise cloud tier, every resource — users, pockets, messages, sessions, notifications — is scoped to a `Workspace`. The `Workspace` document is the root of the tenancy hierarchy. It is created once per organization (or per enterprise deployment) and its ID flows as a foreign key through every other document in the system.

## Tenancy Architecture

PocketPaw EE supports multi-workspace deployments where a single MongoDB instance hosts multiple organizations. Every other document carries a `workspace` or `workspace_id` field that maps back to a `Workspace._id`. This design means:

- Access control checks start with: "does the requesting user have a membership entry for this workspace?"
- Data isolation is enforced at the application layer by always filtering on `workspace_id`.
- Workspace deletion cascades by either hard-deleting all associated documents or by marking `deleted_at` on the workspace and excluding it from all queries.

## `slug` Field

The `slug` is a unique URL-safe identifier for the workspace (e.g., `acme-corp`). It is indexed with `Indexed(str, unique=True)`, enforcing uniqueness at the database level. The slug is used in:

- API routes that need human-readable workspace identifiers without exposing ObjectIds.
- Invitation URLs where the workspace slug makes the destination legible.
- Multi-tenant sub-domain routing if the deployment uses per-workspace subdomains.

## Plan and Seats

`plan` defaults to `"team"` and accepts values from the license system: `team`, `business`, or `enterprise`. `seats` defaults to 5. These fields are set at workspace creation time by the billing/license layer and control feature availability and member capacity throughout the EE tier.

The model itself does not enforce seat limits — that enforcement lives in the invite or member-add service, which checks `workspace.seats` against the current member count before allowing new members.

## `WorkspaceSettings` Sub-Document

**`default_agent`** — An optional agent ID that is pre-assigned to new pockets created in this workspace. This allows enterprise admins to ensure all pockets start with the organization's configured agent without requiring users to select it manually.

**`allow_invites`** — A boolean that workspace admins can disable to freeze membership. When `False`, all invite creation attempts should be rejected by the service layer, regardless of the inviter's role.

**`retention_days`** — Optional message retention policy. `None` means keep forever; setting a value triggers automated pruning of messages older than `retention_days` days. The model stores the policy; enforcement is the responsibility of a background job.

## Soft Deletion

`deleted_at: datetime | None = None` follows the same soft-delete pattern used across PocketPaw models. A workspace marked deleted is hidden from all listings but its data is preserved for a configurable grace period before hard deletion. This prevents accidental data loss from mis-clicks and allows recovery within the grace window.

## Known Gaps

- `plan` is an unvalidated string — the model does not enforce that only known plan values are stored.
- `seats` enforcement is entirely in the service layer; the model will accept `seats=0` or negative values.
- `retention_days` policy is stored but not automatically enforced by this model; requires an external job.
- No index on `owner` — finding all workspaces owned by a user requires a full collection scan.