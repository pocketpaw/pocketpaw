---
{
  "title": "Auth Domain Package Init with Backward-Compatible Re-exports (ee/cloud/auth/)",
  "summary": "Re-exports all public auth symbols from core.py and the router. Exists for backward compatibility so code importing from `ee.cloud.auth` directly continues to work after the module was split into core.py, router.py, and service.py.",
  "concepts": [
    "re-export",
    "backward compatibility",
    "auth domain",
    "package init"
  ],
  "categories": [
    "architecture",
    "enterprise",
    "cloud",
    "auth"
  ],
  "source_docs": [
    "7bc5657b90e3b347"
  ],
  "backlinks": null,
  "word_count": 138,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Auth Domain Package Init

## Purpose

This `__init__.py` re-exports key symbols from `ee.cloud.auth.core` and `ee.cloud.auth.router` for backward compatibility. When the auth domain was refactored from a single file into multiple modules (core, router, service, schemas), existing imports like `from ee.cloud.auth import fastapi_users` needed to keep working.

## Exported Symbols

From `core.py`:
- `current_active_user`, `current_optional_user` — FastAPI dependency functions
- `fastapi_users` — the FastAPIUsers instance
- `get_jwt_strategy`, `get_user_manager`, `get_user_db` — factory functions
- `cookie_backend`, `bearer_backend` — auth backend configurations
- `UserRead`, `UserCreate` — Pydantic schemas
- `UserManager` — user lifecycle manager
- `seed_admin` — admin user seeding function
- `SECRET`, `TOKEN_LIFETIME` — auth constants

From `router.py`:
- `router` — the FastAPI APIRouter instance

## Known Gaps

- The `# noqa: F401` comments suppress unused-import warnings but also suppress legitimate detection of actually-unused re-exports if the API surface shrinks.