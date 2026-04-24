---
{
  "title": "Cloud Files FastAPI Router Integration Tests: Tree and Browse Endpoints",
  "summary": "This module runs end-to-end HTTP integration tests against the FastAPI router built by `build_router`, covering tree structure rendering, entry browsing, 404 for unknown mounts, and 403 for cross-workspace access attempts. It uses HTTPX's `ASGITransport` to drive the full request-response cycle in-process without a running server.",
  "concepts": [
    "FastAPI router",
    "ASGITransport",
    "build_router",
    "tree endpoint",
    "browse endpoint",
    "workspace isolation",
    "MountNotFound",
    "HTTP integration tests",
    "HTTPX",
    "tree cache invalidation"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "API Layer",
    "Integration Tests",
    "test"
  ],
  "source_docs": [
    "4dc551ded181bce5"
  ],
  "backlinks": null,
  "word_count": 487,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_router_tree_browse.py` is the integration test layer for the cloud files HTTP API. Unlike unit tests that call individual functions, these tests construct a real `FastAPI` application, wire the router, and issue HTTP requests through `httpx.AsyncClient` with `ASGITransport`. This approach exercises the full stack: routing, dependency injection, error translation, and JSON serialization.

## Test Infrastructure

Two fixtures manage test isolation:

- **`_clear_tree_cache` (autouse)**: calls `invalidate_tree_cache()` before and after each test. Without this, a cached tree built by one test leaks into the next, producing false positives or negatives depending on test order.
- **`_ctx_factory`**: provides a consistent `RequestContext` (`user_id="u1"`, `workspace_id="ws_1"`) to the router's dependency injection, simulating an authenticated user.

## Test Breakdown

### `test_get_tree_returns_folder_nodes`

Builds a registry with one `uploads` provider mounted at `/My Files`, then issues `GET /api/v1/files/tree`. The test asserts HTTP 200, checks that the first child node is named `"My Files"`, and confirms that `warnings` is empty (no provider failures).

```python
assert body["children"][0]["name"] == "My Files"
assert body["warnings"] == []
```

This validates the full pipeline: mount config -> provider fan-out -> folder node assembly -> JSON response.

### `test_get_browse_returns_entries`

Seeds a `FakeProvider` with one entry and issues `GET /api/v1/files/browse?mount=/My Files`. The test asserts HTTP 200 and that the response body contains exactly one item with the correct `id`.

```python
assert len(body["items"]) == 1
assert body["items"][0]["id"] == "uploads:a"
```

### `test_get_browse_unknown_mount_is_404`

Issues `GET /api/v1/files/browse?mount=/nope` against an empty registry. The test expects HTTP 404 with a structured error detail of `"files.mount_not_found"`. This verifies that `MountNotFound` exceptions are correctly translated to 404 responses by the router's error handler.

```python
assert r.status_code == 404
assert r.json()["detail"] == "files.mount_not_found"
```

Without this, a `MountNotFound` propagating as an unhandled exception would produce a 500, leaking internal stack trace details to the caller.

### `test_get_browse_workspace_mismatch_is_403`

Issues a browse request with `workspace_id=ws_other` in the query params, while the context factory returns `workspace_id=ws_1`. The test expects HTTP 403 with `"files.workspace_mismatch"`. This is the cross-tenant isolation check: a user from workspace A must never browse workspace B's mounts by passing a different workspace ID.

### `test_get_tree_workspace_mismatch_is_403`

The same workspace mismatch check for the `/tree` endpoint, confirming isolation is enforced at both the listing and tree-traversal level.

## Why Integration Tests at This Layer

Unit tests on `browse_mount` and `build_tree` confirm the core logic, but they cannot verify:

1. That `MountNotFound` is translated to 404 (not 500) by the FastAPI exception handler.
2. That the workspace mismatch check runs before hitting the provider.
3. That the JSON response shape matches what clients expect.

The ASGI transport approach gives full request-response fidelity without network overhead or a running server process.

## Known Gaps

There are no tests for authenticated requests with real JWT validation -- `_ctx_factory` bypasses auth entirely. The workspace mismatch check is tested at the HTTP layer but the mechanism may behave differently when the auth middleware is in the stack. This is presumably covered by e2e tests or auth-specific test modules.
