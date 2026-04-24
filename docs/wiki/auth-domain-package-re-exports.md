---
{
  "title": "Auth Domain Package Re-Exports",
  "summary": "The auth domain `__init__.py` is a backward-compatibility shim that re-exports the entire public surface of `auth.core` and `auth.router` from the `ee.cloud.auth` namespace, so existing callers don't need to update their imports if the internal module structure changes.",
  "concepts": [
    "re-exports",
    "backward compatibility",
    "auth domain",
    "fastapi-users",
    "public API facade",
    "noqa F401",
    "module structure"
  ],
  "categories": [
    "auth",
    "architecture",
    "API"
  ],
  "source_docs": [
    "0f77d75cd3d8bfeb"
  ],
  "backlinks": null,
  "word_count": 217,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Re-Exports Matter

Early versions of PocketPaw's enterprise auth exposed symbols directly from `ee.cloud.auth.core`. As the domain grew, code was split into `core.py`, `router.py`, `service.py`, and `schemas.py`. Without the re-export shim, every existing import like `from ee.cloud.auth import current_active_user` would break.

The `__init__.py` preserves the old import path as a stable public API while allowing the internal layout to evolve freely. This is the Python equivalent of a facade or public header.

## What Is Re-Exported

From `ee.cloud.auth.core`:
- `SECRET`, `TOKEN_LIFETIME` — JWT configuration constants
- `UserCreate`, `UserRead` — fastapi-users schema classes
- `UserManager` — the custom user manager
- `bearer_backend`, `cookie_backend` — the two auth transport backends
- `current_active_user`, `current_optional_user` — FastAPI dependency callables
- `fastapi_users` — the central FastAPIUsers instance
- `get_jwt_strategy`, `get_user_db`, `get_user_manager` — dependency factories
- `ensure_default_agent_all_workspaces`, `seed_admin`, `seed_default_agent`, `seed_workspace` — startup seeding functions

From `ee.cloud.auth.router`:
- `router` — the FastAPI router for all auth HTTP endpoints

## `noqa: F401` Usage

All imports carry `# noqa: F401` to suppress "imported but unused" linter warnings. The re-exports are used by callers of this package, not by code within `__init__.py` itself, so static analysis tools cannot infer their usage automatically.

## Known Gaps

- None specific to this file. The pattern is intentional and the re-export list is exhaustive for the current public surface.