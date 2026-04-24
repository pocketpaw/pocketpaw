---
{
  "title": "Enterprise Authentication Core — JWT, User Management, and Startup Seeding",
  "summary": "`auth/core.py` implements PocketPaw Enterprise's authentication system using `fastapi-users` with dual JWT transports (cookie for browser, bearer for API/Tauri), and provides idempotent startup seeding functions that ensure a default admin, workspace, chat group, and default agent exist before the first user logs in.",
  "concepts": [
    "fastapi-users",
    "JWT",
    "cookie transport",
    "bearer transport",
    "UserManager",
    "seed_admin",
    "seed_workspace",
    "seed_default_agent",
    "ensure_default_agent_all_workspaces",
    "idempotent seeding",
    "AUTH_SECRET"
  ],
  "categories": [
    "auth",
    "security",
    "enterprise"
  ],
  "source_docs": [
    "91cf3b069e109738"
  ],
  "backlinks": null,
  "word_count": 434,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Dual Transport Strategy

Two authentication backends are registered on the same JWT strategy:

- **Cookie transport** (`paw_auth` cookie, SameSite=lax): for browser clients that benefit from automatic credential management. The cookie is marked `secure=False` with a comment noting this should be `True` in production with HTTPS.
- **Bearer transport** (Authorization header): for API clients, the Tauri desktop app, and programmatic access where cookie handling is inconvenient.

Both transports use the same `JWTStrategy` with a 7-day lifetime. A single `SECRET` (sourced from `AUTH_SECRET` env var, defaulting to a dev placeholder) signs all tokens. The default `"change-me-in-production-please"` value is a deliberate signal that the secret must be overridden in deployment.

## UserManager Customisation

`UserManager` extends fastapi-users' `BaseUserManager` with two hooks:
- `on_after_register`: logs new registrations at INFO level
- `on_after_login`: logs logins at DEBUG level

No webhook calls or notification emails are sent — this is a deliberate simplification for the current self-hosted deployment model.

## Startup Seeding Chain

The seeding functions form a dependency chain intended to run at startup:

```
seed_admin() → seed_workspace(admin) → seed_default_agent(workspace_id, owner_id)
```

Each step is **idempotent**: it checks for existing data and returns early rather than inserting duplicates. This means the seeding chain can run on every boot without risk.

`seed_workspace()` does more than create a workspace — it also creates a "General" chat group and calls `seed_default_agent()`. The group and agent failures are non-fatal (wrapped in `try/except`) so a bug in group creation doesn't prevent the workspace from being seeded.

## `ensure_default_agent_all_workspaces()`

This back-fill function solves a deployment timeline problem: workspaces created before agent seeding was added never got a default `pocketpaw` agent. On every boot, this function iterates all workspaces and calls `seed_default_agent()` for each. `seed_default_agent()` is idempotent (returns `(existing, False)` if the agent already exists), so the back-fill is safe to run repeatedly. The return value counts only newly-created agents so log output doesn't misleadingly report the workspace count on subsequent boots.

## The Default Agent Purpose

`seed_default_agent()` creates an agent with `slug="pocketpaw"` in each workspace. This agent is the DM target for users — the frontend uses its `_id` as the DM room identifier, replacing the legacy `__paw-runtime-dm__` string sentinel. Session documents carry `agent=<this agent's id>` so per-agent conversation history works correctly.

## Known Gaps

- `cookie_secure=False` must be changed to `True` for any HTTPS deployment. There is no environment-variable switch for this currently.
- The `SECRET` default (`"change-me-in-production-please"`) will silently function in production if the env var is not set, issuing tokens that are only as secure as the secret entropy.
- Email verification is not enforced (`is_verified=True` is set unconditionally during admin seeding).