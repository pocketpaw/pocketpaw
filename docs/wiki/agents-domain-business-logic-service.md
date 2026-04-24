---
{
  "title": "Agents Domain Business Logic Service",
  "summary": "The `AgentService` is the stateless business logic layer for the agents domain, handling agent creation (with slug uniqueness enforcement and eager soul materialisation), updates, discovery, and the new scope get/set operations introduced for the ScopePicker feature.",
  "concepts": [
    "AgentService",
    "stateless service",
    "slug uniqueness",
    "soul materialisation",
    "scope assignment",
    "agent discovery",
    "visibility rules",
    "Beanie ODM",
    "defence-in-depth",
    "agent pool"
  ],
  "categories": [
    "agents",
    "business logic",
    "enterprise"
  ],
  "source_docs": [
    "2edf1cd9f2c16003"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Design: Stateless Service Class

`AgentService` uses only `@staticmethod` methods — there is no instance state. This is intentional: the service layer doesn't hold connections or caches; those belong to the database layer (`Agent` Beanie ODM) and the singleton stores. Stateless methods are easier to test, easier to mock, and avoid lifecycle management concerns.

## Slug Uniqueness Enforcement

Agent slugs must be unique within a workspace. The create method checks for an existing agent with the same workspace+slug pair before inserting:

```python
existing = await Agent.find_one(Agent.workspace == workspace_id, Agent.slug == body.slug)
if existing:
    raise ConflictError("agent.slug_taken", ...)
```

This is a read-before-write race (two concurrent creates with the same slug could both pass the check), but the MongoDB collection should have a unique compound index on `(workspace, slug)` as the true enforcement layer. The pre-check exists to produce a user-readable `409 Conflict` rather than a raw MongoDB duplicate-key error.

## Eager Soul Materialisation

When a new agent is created with `soul_enabled=True`, the service immediately calls `get_agent_pool().ensure_soul(agent)`. This creates the `.soul` file on disk before the agent's first conversation, preventing a latency spike on the first chat message:

```python
if config.soul_enabled:
    try:
        await get_agent_pool().ensure_soul(agent)
    except Exception:
        logger.warning("Eager soul creation failed", exc_info=True)
```

The failure is non-fatal — the agent pool will retry lazily on first use. The `try/except` prevents a soul-creation bug from blocking agent creation entirely.

## Scope Get/Set

`get_scopes()` and `set_scopes()` are the service methods backing the `/agents/{id}/scope` endpoints. `set_scopes()` re-runs `normalise_and_validate_scopes()` even though the router already validated through `ScopeAssignmentRequest`. The double validation is explicit defence-in-depth: a fleet installer calling `set_scopes()` directly would skip the Pydantic validation, so the service enforces it independently.

## Discovery with Visibility Rules

`discover()` implements a three-visibility model:
- `private`: only the requesting user's own agents
- `workspace`: all agents in the workspace regardless of owner
- `public`: all public agents across all workspaces
- Default (no filter): union of all three — own + workspace-visible + public

Pagination uses MongoDB's `.skip().limit()` pattern. The page/size parameters come from `DiscoverRequest`.

## `_agent_response()` Mapping

The private helper converts `Agent` ODM documents to frontend-compatible dicts, translating internal field names to the camelCase keys the frontend expects (`createdAt` → `createdOn`, `slug` → `uname`). Centralising this mapping prevents the field-naming convention from leaking into individual methods.

## Known Gaps

- The read-before-write on slug uniqueness is a race condition. A unique compound index on `(workspace, slug)` in MongoDB would be the definitive fix.
- `update()` checks `agent.owner != user_id` but the router also applies `require_agent_owner_or_admin`, which means admins can reach the endpoint but the service will reject them unless they are also the owner. This looks like an inconsistency — admins should be able to update agents they don't own.