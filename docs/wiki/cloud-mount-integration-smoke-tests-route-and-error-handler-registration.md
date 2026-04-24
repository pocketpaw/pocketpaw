---
{
  "title": "Cloud Mount Integration Smoke Tests: Route and Error Handler Registration",
  "summary": "This smoke test suite verifies that `mount_cloud()` successfully registers all expected domain route groups (auth, workspaces, agents, chat, pockets, sessions, WebSocket, license) and the `CloudError` exception handler on a fresh FastAPI app. It acts as a fast regression guard ensuring no route group is accidentally omitted from the mount function.",
  "concepts": [
    "mount_cloud",
    "FastAPI route registration",
    "smoke test",
    "exception handler",
    "CloudError",
    "WebSocket endpoint",
    "route count",
    "domain routing",
    "integration test"
  ],
  "categories": [
    "testing",
    "integration",
    "API routing",
    "test"
  ],
  "source_docs": [
    "578db9a2d2936b60"
  ],
  "backlinks": null,
  "word_count": 419,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`mount_cloud(app)` is the single entry point for attaching all PocketPaw cloud API routes to a FastAPI application. This test file verifies that calling it produces a fully wired app with the correct route groups, the WebSocket endpoint, the license endpoint, and the global `CloudError` exception handler.

## Why This Exists

As the cloud API grows — new domains, new versioned endpoints — it's easy to forget to wire a new router into `mount_cloud`. Without a smoke test, the omission would only surface when a client hit a 404 in production. This file acts as a fast, cheap regression guard that runs on every CI push.

## Test Structure

Every test builds a fresh `FastAPI()` instance, calls `mount_cloud(app)`, and inspects `app.routes` via the `_get_route_paths` helper:

```python
def _get_route_paths(app: FastAPI) -> list[str]:
    return [route.path for route in app.routes if hasattr(route, "path")]
```

This helper intentionally uses a simple `hasattr` check rather than isinstance filtering, because FastAPI mounts WebSocket routes and HTTP routes under different route types — both need to be counted.

## Domain Coverage

Each test checks for a path substring, not an exact path, to avoid brittleness as endpoint paths evolve:

- **Auth** — `/auth` (fastapi-users mounts at `/api/v1/auth/*`)
- **Workspaces** — `/workspaces`
- **Agents** — `/agents`
- **Chat** — `/chat`
- **Pockets** — `/pockets`
- **Sessions** — `/sessions`
- **WebSocket** — `ws/cloud`
- **License** — `/api/v1/license` (exact match, because this is a single endpoint)

## Exception Handler Registration

`test_cloud_error_handler_registered` checks `app.exception_handlers` directly:

```python
assert CloudError in app.exception_handlers
```

This matters because FastAPI's exception handler registration is not automatic — if `mount_cloud` forgets to call `app.add_exception_handler(CloudError, ...)`, all CloudError subclasses would bubble up as unformatted 500 responses instead of the typed JSON errors the client expects.

## Route Count Sanity Check

`test_total_route_count` asserts `len(paths) >= 40`. This catches wholesale route removal — if someone accidentally comments out an `include_router` call, the count drops and the test fails. The threshold is intentionally loose (40+) to avoid brittleness as endpoints are added, but tight enough to catch a missing domain.

## Known Gaps

- **No version prefix assertions** — Tests check for path substrings like `/pockets` but don't verify the full path including `/api/v1/`. A route accidentally mounted at `/v2/pockets` would pass these tests.
- **No method assertions** — The tests don't verify that specific HTTP methods (GET, POST, etc.) are registered, only that the path exists.
- **WebSocket test** — Uses `"ws/cloud" in p` which would match both `/ws/cloud` and `/api/ws/cloud`. The exact mount path is not pinned.
