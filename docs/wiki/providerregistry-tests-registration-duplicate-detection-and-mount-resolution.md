---
{
  "title": "ProviderRegistry Tests: Registration, Duplicate Detection, and Mount Resolution",
  "summary": "This module tests `ProviderRegistry`, the central index that maps provider IDs to `FolderProvider` implementations and resolves virtual mount paths to their owning providers. Key behaviors tested include duplicate registration rejection, longest-prefix mount resolution, and template variable substitution during path matching.",
  "concepts": [
    "ProviderRegistry",
    "mount resolution",
    "longest prefix matching",
    "FolderProvider",
    "MountNotFound",
    "variable substitution",
    "provider registration",
    "duplicate detection",
    "cloud files routing"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Provider System",
    "Mount Resolution",
    "test"
  ],
  "source_docs": [
    "79561b42b785748c"
  ],
  "backlinks": null,
  "word_count": 509,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_registry.py` validates `ProviderRegistry` from `ee.cloud.files.registry`. The registry serves two distinct roles:

1. **Provider directory**: stores and retrieves `FolderProvider` instances by their string `provider_id`.
2. **Mount resolver**: given a virtual path and a variables dict, finds the most specific `MountConfig` whose template matches as a prefix.

Both roles are critical: incorrect provider lookup or mount resolution causes either `MountNotFound` errors or, worse, routing requests to the wrong provider.

## Test Breakdown

### `test_register_and_get`

Basic round-trip: register a `FakeProvider` under `"uploads"`, retrieve it by ID, assert it is the same object.

```python
reg.register(p)
assert reg.get("uploads") is p
```

The `is` identity check (not `==`) ensures no copying or wrapping occurs -- the registry must return the exact instance for provider state to be shared correctly across calls.

### `test_register_duplicate_raises`

Registering two providers with the same `provider_id` must raise `ValueError`. Without this guard, the second registration silently overwrites the first, causing any in-flight operations on the first provider's state to reference the wrong object.

```python
reg.register(FakeProvider("uploads"))
with pytest.raises(ValueError):
    reg.register(FakeProvider("uploads"))
```

This is an idempotency guard -- it prevents accidental double-initialization during server startup, which could happen if provider registration code runs in a module-level block that is imported twice.

### `test_resolve_mount_longest_prefix`

Two mounts are configured: `/X` (provider `a`) and `/X/Y` (provider `b`). A request for `/X/Y/inside` must resolve to provider `b` because `/X/Y` is a longer, more specific prefix than `/X`.

```python
got = reg.resolve_mount(path="/X/Y/inside", variables={})
assert got.provider_id == "b"
```

This is the key routing invariant. Without longest-prefix semantics, a nested mount like `/Workspaces/ws_1/KB` could be incorrectly routed to a top-level `/Workspaces` mount if one exists.

### `test_resolve_mount_missing_raises`

When no configured mount template matches the requested path, `resolve_mount` raises `MountNotFound`. This converts a registry lookup failure into the canonical structured error that the router translates to HTTP 404.

### `test_resolve_mount_substitutes_variables`

A mount template with a `{workspace_id}` placeholder is resolved with `{"workspace_id": "ws_1"}`. The test confirms that the resolved mount's `path` is the substituted string, and that routing succeeds.

```python
got = reg.resolve_mount(
    path="/Workspaces/ws_1/KB/doc",
    variables={"workspace_id": "ws_1"}
)
assert got.provider_id == "kb"
assert got.path == "/Workspaces/ws_1/KB"
```

This is the mechanism that makes per-workspace mounts work: the template is static in config, but the actual path is materialized per-request using context variables.

## Design Implications

The registry is intentionally immutable after initialization -- providers are registered once at startup, and the registry raises on duplicates rather than allowing hot-swapping. This makes the registry safe to use as a shared, unguarded singleton across concurrent async requests.

Mount resolution is also stateless per-call: it takes a `variables` dict rather than reading from the registry's own state, which keeps the resolution logic pure and easily testable in isolation.

## Known Gaps

There is no test for what happens when two mount templates become identical after variable substitution (e.g., two templates both resolve to `/Workspaces/ws_1/KB` with different variable sets). The registry may return an arbitrary winner in that case. Additionally, there is no test for `resolve_mount` with a path that exactly matches a template (no trailing segment), though this likely works given the prefix-matching logic.
