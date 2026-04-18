---
{
  "title": "Workspace Model: Organization-Level Tenant with Plan and Settings",
  "summary": "Beanie document model for organization workspaces — the top-level tenant boundary in PocketPaw Enterprise. Each workspace has a unique slug, an owner, a licensing plan (team/business/enterprise), seat limits, and configurable settings including default agent and data retention.",
  "concepts": [
    "Workspace",
    "WorkspaceSettings",
    "tenant",
    "slug",
    "licensing",
    "plan",
    "seats",
    "retention",
    "soft delete"
  ],
  "categories": [
    "Models",
    "Workspace Management",
    "Data Layer",
    "Multi-Tenancy"
  ],
  "source_docs": [
    "6ac29eab827b32eb"
  ],
  "backlinks": null,
  "word_count": 364,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Workspace Model: Organization-Level Tenant with Plan and Settings

## Purpose

The `Workspace` model is the top-level organizational unit in PocketPaw Enterprise. Every other entity (groups, pockets, sessions, messages, files) is scoped to a workspace. It is the tenant boundary — data isolation between organizations happens at the workspace level.

## Design Decisions

### Unique Slug Index
The `slug` field is `Indexed(str, unique=True)`, ensuring globally unique workspace URLs. Slugs are used in URLs (e.g., `/workspace/acme-corp/`) and must be unique across all deployments, not just within a single organization.

### Plan and Seat Licensing
The `plan` field (`team`/`business`/`enterprise`) and `seats` count tie into the licensing system. The comment "from license" indicates these values are populated from an external license key, not user input. The `seats` default of 5 suggests the free/team tier starts with 5 seats.

### WorkspaceSettings as Embedded Model
Settings are embedded as a subdocument rather than stored as a flat dict. This provides type safety and default values:
- `default_agent`: The AI agent assigned to new groups/pockets by default
- `allow_invites`: Whether workspace members can invite others
- `retention_days`: Optional data retention policy (None = keep forever)

### Soft Delete
The `deleted_at` timestamp enables soft deletion. Since workspaces are the top-level tenant, hard deletion would cascade to all child entities. Soft deletion preserves data integrity and enables recovery.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `name` | `str` | Display name |
| `slug` | `Indexed(str, unique=True)` | URL-safe unique identifier |
| `owner` | `str` | Admin user ID who created it |
| `plan` | `str` | License tier: `team`, `business`, `enterprise` |
| `seats` | `int` | Maximum allowed members |
| `settings` | `WorkspaceSettings` | Configuration subdocument |
| `deleted_at` | `datetime | None` | Soft deletion timestamp |

## Known Gaps

- No `plan` field validation (no `Field(pattern=...)`) — unlike other string-enum fields in the codebase, the plan value is not constrained at the model level.
- No `members_count` denormalized field — checking if a workspace is at seat capacity requires querying the users collection.
- The `retention_days` setting exists but there is no visible background job or TTL mechanism to enforce it.
