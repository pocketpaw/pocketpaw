---
{
  "title": "Cloud Files Tree Cache Tests: TTL, Invalidation, and Multi-Tenant Key Isolation",
  "summary": "This module tests `CachedTreeBuilder`, the in-process 30-second cache layer over `build_tree` that prevents repeated provider fan-outs on every tree request. Tests validate that cache hits suppress redundant `list_mounts` calls, that explicit invalidation forces a rebuild, that TTL expiry triggers a new fan-out, and that clearing the cache invalidates all per-user keys.",
  "concepts": [
    "CachedTreeBuilder",
    "tree cache",
    "TTL",
    "invalidate_tree_cache",
    "CountingProvider",
    "cache key",
    "multi-tenant isolation",
    "clock injection",
    "provider fan-out",
    "list_mounts"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Caching",
    "Performance",
    "test"
  ],
  "source_docs": [
    "b44c6daf2b6aa72b"
  ],
  "backlinks": null,
  "word_count": 593,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_tree_cache.py` tests `CachedTreeBuilder` from `ee.cloud.files.tree`, a caching wrapper that suppresses repeated calls to `list_mounts` across providers within a configurable TTL window (default: 30 seconds). The tests use a `CountingProvider` to track how many times `list_mounts` is called, and a controllable clock to simulate time passage without actual sleeps.

## Why the Cache Exists

The `/tree` endpoint is called by the frontend on every page load and on every file operation that might change the tree structure. Each call fans out to every registered provider, calling `list_mounts` on each. If the system has five providers, every tree request triggers five async calls to external services (S3, Google Drive APIs, MongoDB, etc.).

A 30-second in-process cache means that during a typical user session, the provider fan-out happens at most twice per minute per user, regardless of how many tree requests the frontend issues. This keeps the UI responsive while avoiding rate-limit pressure on external providers.

## CountingProvider

The tests use `CountingProvider`, a minimal `FolderProvider` stub that increments `list_mounts_calls` each time `list_mounts` is called. All other operations either return empty results or raise `NotImplementedError`. The counter is the observable metric for all cache assertions.

## Test Breakdown

### `test_cache_hits_within_ttl_do_not_refanout`

Calls `builder.build` three times: at t=1000, again immediately, and after advancing the clock by 10 seconds (still within the 30s TTL). Asserts that `list_mounts_calls == 1`.

```python
await builder.build(ctx=_ctx())
await builder.build(ctx=_ctx())
now[0] += 10  # still inside 30s window
await builder.build(ctx=_ctx())
assert prov.list_mounts_calls == 1
```

This confirms the fundamental cache behavior: identical context within TTL does not trigger a provider fan-out.

### `test_invalidate_forces_refanout`

After the first build, calls `invalidate_tree_cache(user_id="u1", workspace_id="ws_1")` to explicitly evict the cache entry for that user+workspace key. A subsequent build must re-fan-out, bringing `list_mounts_calls` to 2.

This is the mechanism used when a file is uploaded, renamed, or deleted -- the operation handler calls `invalidate_tree_cache` so the next tree request reflects the change immediately, without waiting for the TTL to expire.

### `test_ttl_expiry_forces_refanout`

Advances the clock by 31 seconds (past the 30s TTL) between two builds. Asserts `list_mounts_calls == 2`. This verifies that stale cache entries are not served indefinitely.

```python
now[0] += 31.0  # past TTL
await builder.build(ctx=_ctx())
assert prov.list_mounts_calls == 2
```

### `test_invalidate_all_clears_every_key`

Builds the tree for two different contexts (`u1/ws_1` and `u2/ws_2`), seeding two cache entries. Calls `invalidate_tree_cache()` with no arguments (global invalidation). Rebuilds both contexts and asserts `list_mounts_calls == 4` (2 initial + 2 post-invalidation).

```python
invalidate_tree_cache()
await builder.build(ctx=ctx_a)
await builder.build(ctx=ctx_b)
assert prov.list_mounts_calls == 4
```

Global invalidation is used during provider reconfiguration (e.g., adding a new mount) when all users' cached trees are stale simultaneously.

## Cache Key Design

The cache is keyed per `(user_id, workspace_id)`, not globally. This ensures that user A's tree (which may include personal files) does not bleed into user B's cache entry. The `_clear_cache` autouse fixture calls `invalidate_tree_cache()` before and after each test to prevent cross-test contamination.

## Controllable Clock Pattern

Rather than mocking `time.time` globally, `CachedTreeBuilder` accepts a `clock` callable. Tests pass `lambda: now[0]` where `now` is a mutable list, allowing the test to advance time by mutating `now[0]`. This avoids the fragility of monkeypatching built-ins and makes the TTL logic trivially testable without `asyncio.sleep`.

## Known Gaps

There are no tests for concurrent builds with the same cache key (race between two simultaneous requests that both miss the cache). The implementation may or may not serialize these, and a stampede could cause N simultaneous fan-outs before the first result is cached. This is a known gap in async cache implementations without a dedicated lock or `asyncio.Lock` per key.
