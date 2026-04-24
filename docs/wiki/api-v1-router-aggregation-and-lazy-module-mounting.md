---
{
  "title": "API v1 Router Aggregation and Lazy Module Mounting",
  "summary": "The `__init__.py` for PocketPaw's v1 API layer is responsible for registering every domain router onto the FastAPI application via a single `mount_v1_routers()` function. It uses deferred imports to sidestep circular-dependency problems that arise when a large plugin-style codebase wires itself up at startup.",
  "concepts": [
    "FastAPI router",
    "lazy import",
    "circular import prevention",
    "domain router",
    "API v1",
    "mount_v1_routers",
    "Fleet router",
    "Automations router",
    "Retrieval router",
    "Widgets router",
    "backward compatibility",
    "startup event"
  ],
  "categories": [
    "API",
    "Architecture",
    "FastAPI"
  ],
  "source_docs": [
    "1ee71cf54126cece"
  ],
  "backlinks": null,
  "word_count": 417,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's REST API is organized around domain-specific routers — one file per feature area (auth, channels, chat, identity, etc.). Rather than importing and registering each router directly in the application factory, all v1 registrations are delegated to a single orchestrator: `mount_v1_routers(app)` in `src/pocketpaw/api/v1/__init__.py`.

## Why a Central Aggregator?

As a project grows, the number of routers multiplies. Hardcoding each import at the top of the app factory creates a tree of cross-module dependencies that Python resolves eagerly at import time. In PocketPaw's architecture — where routers reference services, services reference config, and config references almost everything — that eager resolution triggers circular import errors before the application even starts.

The aggregator solves this with a **lazy-import pattern**. The router list `_V1_ROUTERS` is a plain Python list of `(module_path, attr_name, tag)` tuples. No module is actually imported until `mount_v1_routers()` is called at runtime (typically during the FastAPI `startup` event), by which point all modules have been fully initialized.

```python
_V1_ROUTERS: list[tuple[str, str, str]] = [
    ("pocketpaw.api.v1.auth", "router", "Auth"),
    ("pocketpaw.api.v1.channels", "router", "Channels"),
    # ... 20+ more entries
]
```

## Router Growth and Feature Flags

The changelog inside the file shows an iterative growth pattern. Key additions include:

- **Automations router** (2026-03-30): Enterprise rule-based pocket automations, added as a discrete domain.
- **Fleet router** (2026-04-16): Exposes `GET /api/v1/fleet/templates` and `POST /api/v1/fleet/install` for `paw-enterprise`'s `InstallFleetPanel` to call against a running PocketPaw instance.
- **Retrieval router** (2026-04-16): Journal-backed retrieval and graduation projection, superseding held PRs #936/#937.
- **Widgets router** (2026-04-16): Journal-backed widget graduation and co-occurrence, superseding held PRs #941/#942.

Each new domain is simply appended to the `_V1_ROUTERS` list — no other file needs to change.

## Backward Compatibility Alias

Legacy `dashboard.py` endpoints mounted at `/api/` continue to exist as backward-compatible aliases. The new canonical path for all features is `/api/v1/`. The aggregator comment documents this split explicitly so future contributors don't inadvertently break clients pinned to the old paths.

## `TYPE_CHECKING` Guard

The `FastAPI` type annotation on `mount_v1_routers(app)` is imported only under `if TYPE_CHECKING:`. This ensures the type checker knows the signature without forcing a runtime import of FastAPI before the app itself is constructed — a subtle but important detail in environments that instrument the import graph.

## Known Gaps

None flagged explicitly in the source. However, the `_V1_ROUTERS` list has grown to 20+ entries and is maintained manually. There is no automated test that verifies every expected router is registered, meaning a merge conflict or accidental deletion could silently drop an entire API domain at startup.