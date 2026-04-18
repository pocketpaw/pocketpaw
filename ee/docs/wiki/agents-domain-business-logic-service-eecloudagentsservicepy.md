---
{
  "title": "Agents Domain Business Logic Service (ee/cloud/agents/service.py)",
  "summary": "The stateless service layer for agent CRUD operations, enforcing business rules like slug uniqueness, owner-only mutations, and multi-level visibility-based discovery. Uses Beanie ODM for MongoDB persistence.",
  "concepts": [
    "AgentService",
    "Beanie ODM",
    "MongoDB",
    "slug uniqueness",
    "visibility rules",
    "owner authorization",
    "CRUD",
    "discovery"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "business logic",
    "agents",
    "authorization"
  ],
  "source_docs": [
    "c11bf2f911508a17"
  ],
  "backlinks": null,
  "word_count": 502,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agents Domain Business Logic Service

## Purpose

`AgentService` encapsulates all business logic for agent management. Following PocketPaw's domain-driven pattern, the service is stateless — each method is a `@staticmethod` that receives its dependencies as parameters and interacts with the database via Beanie ODM.

## Key Operations

### Create

Agent creation enforces **workspace-scoped slug uniqueness**. Before inserting, it queries for an existing agent with the same workspace + slug combination. If found, it raises `ConflictError` — this prevents URL collisions since agents are addressable by slug.

The config construction conditionally includes optional fields: `temperature`, `max_tokens`, `tools`, `trust_level`, `soul_values`, and `soul_ocean` are only set when provided. This ensures defaults from `AgentConfig` aren't overridden with explicit `None` values.

The default `soul_archetype` falls back to `f"The {body.name}"` — so an agent named "Sales Bot" gets archetype "The Sales Bot" unless explicitly overridden.

### List

Supports optional name search via MongoDB's `$regex` with case-insensitive option. Returns all matching agents in the workspace.

### Get / Get By Slug

Simple lookups that raise `NotFound` on miss. `get_by_slug` searches within a workspace scope.

### Update

**Owner-only**: compares the requesting user's ID against the agent's `owner` field. Non-owners get `Forbidden`.

Supports two update paths:
1. **Bulk config replacement**: if `body.config` is provided as a dict, it replaces the entire `AgentConfig`
2. **Granular field updates**: individual fields (backend, model, temperature, etc.) are applied to the existing config. This is important for the frontend where a user might change just the temperature without sending the entire config.

The `persona` field maps to `soul_persona` in the config — this name mapping exists because the API uses "persona" (user-friendly) while the internal config uses "soul_persona" (technically precise).

### Delete

**Hard delete** with owner-only check. No soft delete or archival — the agent document is permanently removed.

### Discover

The most complex query logic. Supports four visibility modes:

| Mode | Filter |
|------|--------|
| `private` | User's own agents in current workspace |
| `workspace` | All agents in current workspace |
| `public` | All public agents across all workspaces |
| Default (none specified) | Union: user's own + workspace-visible + all public |

The default mode uses MongoDB's `$or` operator to combine three conditions. This gives users a comprehensive view: their private agents, their workspace's shared agents, and globally public agents.

## Response Serialization

The `_agent_response()` helper builds frontend-compatible dicts. Notable mappings:
- `slug` → `uname` (frontend convention)
- `createdAt` → `createdOn` (ISO format string)
- `updatedAt` → `lastUpdatedOn`

These naming translations bridge MongoDB/Python conventions to the frontend's JavaScript naming expectations.

## Known Gaps

- No pagination on `list_agents()` — returns all agents matching the filter. Large workspaces could see performance issues.
- Hard delete with no soft-delete option means accidental deletions are unrecoverable. Consider a `deleted_at` timestamp pattern.
- The `$regex` name search in `list_agents` and `discover` isn't indexed-friendly — at scale, this should use MongoDB text indexes.
- Owner-only checks in update/delete don't account for workspace admin roles — a workspace admin can't manage agents they don't own.