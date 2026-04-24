---
{
  "title": "Pockets Domain Package",
  "summary": "The pockets `__init__.py` re-exports the `router` symbol from `ee.cloud.pockets.router`, making the router available as `ee.cloud.pockets.router` for registration by the application factory. This single-line re-export pattern keeps the application factory import simple while preserving module boundaries within the domain.",
  "concepts": [
    "domain package",
    "router re-export",
    "application factory",
    "package boundary",
    "FastAPI router registration",
    "noqa"
  ],
  "categories": [
    "pockets",
    "architecture",
    "enterprise-cloud"
  ],
  "source_docs": [
    "9ec67acc68710016"
  ],
  "backlinks": null,
  "word_count": 275,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee/cloud/pockets/` package is the bounded domain for all pocket-related logic in PocketPaw's enterprise cloud tier. Pockets are the primary workspace unit — user-facing canvases with widgets, agents, and automation specs.

## Package Contents

The pockets domain contains several modules:

- **`router.py`** — FastAPI `APIRouter` with CRUD endpoints for pockets.
- **`agent_context.py`** — Pocket data fetcher for agent tool responses.
- **`journal_stream_router.py`** — SSE endpoint for streaming pocket journal events to the RippleGraphWidget.
- Additional service, schema, and utility modules.

## The Re-Export Pattern

```python
from ee.cloud.pockets.router import router  # noqa: F401
```

This single line re-exports `router` at the package level. The `# noqa: F401` suppresses the "imported but unused" lint warning — the import is intentional for its re-export side effect.

The benefit of this pattern is that the application factory can register the pocket router with:

```python
from ee.cloud.pockets import router
app.include_router(router)
```

rather than the more verbose:

```python
from ee.cloud.pockets.router import router
app.include_router(router)
```

This keeps the factory import surface consistent with other domain packages that also re-export `router` from their `__init__.py`.

## Contrast with the Notifications Domain

The notifications `__init__.py` does not re-export anything — consumers import directly from submodules. The pockets package takes the opposite approach, exposing `router` at the package level. This inconsistency suggests the re-export convention was introduced at different points in the codebase's evolution, not as a uniform standard.

## Known Gaps

- Only `router` is re-exported; `agent_context.fetch_pocket_for_agent` and other public symbols are not, requiring full submodule imports from callers.
- The re-export pattern is inconsistent across domain packages (pockets re-exports, notifications does not), which creates minor confusion for developers onboarding to the codebase.