---
{
  "title": "Shared Cross-Cutting Concerns Package Entry Point",
  "summary": "This is the package initialiser for `ee.cloud.shared`, the module that hosts cross-cutting utilities shared across all EE cloud domains. The file contains only a module docstring and no code, making it a namespace anchor that allows other modules to import from `ee.cloud.shared.*` sub-modules like `deps`, `errors`, `events`, and `time`.",
  "concepts": [
    "shared package",
    "cross-cutting concerns",
    "FastAPI dependencies",
    "current_user_id",
    "current_workspace_id",
    "Forbidden",
    "NotFound",
    "event_bus",
    "iso_utc",
    "package namespace",
    "circular import prevention"
  ],
  "categories": [
    "shared utilities",
    "EE cloud",
    "package structure",
    "FastAPI"
  ],
  "source_docs": [
    "e555ef8938efbe6a"
  ],
  "backlinks": null,
  "word_count": 269,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/shared/__init__.py` contains a single module docstring: `"Shared cross-cutting concerns for the PocketPaw cloud module."` There is no re-export surface and no executable code.

## What the `shared` Package Contains

While the `__init__.py` is empty, it anchors a package that other EE cloud domains depend on heavily. The sub-modules typically include:

- **`deps.py`** — FastAPI dependency functions like `current_user_id`, `current_workspace_id`, `require_pocket_owner`, `require_pocket_edit`, and `require_action_any_workspace`. These are injected into routes across the sessions, pockets, and other routers.
- **`errors.py`** — Domain exception classes `Forbidden` and `NotFound` that translate to 403 and 404 HTTP responses.
- **`events.py`** — The legacy `event_bus` object used for internal domain events (distinct from the realtime WebSocket event bus in `ee.cloud.realtime`).
- **`time.py`** — Utility functions like `iso_utc` for consistent datetime serialisation.

## Why a Shared Package?

Without a shared utilities package, cross-domain concerns would be duplicated or co-located with one domain incorrectly. For example, `current_user_id` is used by every router in the EE cloud module. Placing it in `ee.cloud.sessions.deps` would create an awkward import where the pockets router depends on a sessions sub-module. The `shared` namespace makes the dependency direction explicit: domains depend on `shared`, not on each other.

## Design Note

The empty `__init__.py` (no re-exports) is intentional and consistent with the `ee.cloud.realtime` package. Callers must import from the specific sub-module (`from ee.cloud.shared.deps import current_user_id`) rather than from the package root. This avoids the circular import risks that arise when a package `__init__.py` imports from sub-modules that themselves import from other domains.

## Known Gaps

None specific to this file. The package as a whole has no known gaps documented in the codebase.