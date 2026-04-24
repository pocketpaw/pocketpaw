---
{
  "title": "Soul Memories API Tests: Tier Filtering, Limit Clamping, and Entry Serialization",
  "summary": "Unit tests for the `GET /api/v1/soul/memories` endpoint, focusing on the `_collect_tier_entries` helper that queries individual memory tiers, limit clamping to safe bounds, tier filter validation, and graceful handling of absent souls or missing tier stores. Tests operate at the function level to avoid spinning up a full SSE client.",
  "concepts": [
    "soul memories",
    "_collect_tier_entries",
    "memory tiers",
    "episodic",
    "semantic",
    "procedural",
    "limit clamping",
    "_ALLOWED_TIERS",
    "entry normalization",
    "FakeMemoryEntry",
    "SimpleNamespace",
    "soul API"
  ],
  "categories": [
    "testing",
    "soul protocol",
    "memory architecture",
    "API design",
    "test"
  ],
  "source_docs": [
    "3ce9a24b0f32569d"
  ],
  "backlinks": null,
  "word_count": 479,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_soul_memories.py` covers the soul memory introspection API — the endpoint that lets the dashboard and external tools query what a PocketPaw agent currently remembers. The soul stores memories in distinct tiers (episodic, semantic, procedural), and the API exposes a paginated, tier-filtered view.

The tests were created as part of the `feat/cluster-d-agent-reasoning-viewer-plus-soul-memory` feature, which introduced the memory viewer panel in the dashboard.

## _collect_tier_entries Helper

The majority of tests exercise `_collect_tier_entries` directly rather than going through the HTTP layer. This is intentional: the endpoint body is a thin wrapper around this helper, and testing the helper directly produces faster, more focused assertions.

### FakeMemoryEntry and _soul_with_memories

`FakeMemoryEntry` is a minimal Pydantic model with `content` and `importance` fields, mimicking real `MemoryEntry` objects. `_soul_with_memories` constructs a `SimpleNamespace`-based fake soul object with correctly shaped `episodic`, `semantic`, and `procedural` stores — each implementing the iterator protocols the helper expects.

### Episodic Tier

`test_episodic_returns_most_recent_first_slice` verifies episodic memories are returned in reverse-chronological order (most recent first). Agent developers expect the most recent context at the top of the list.

### Semantic Tier

`test_semantic_uses_facts_iterator` verifies the semantic tier uses a `facts()` iterator rather than a raw list, reflecting the semantic store's internal indexing structure.

### Procedural Tier

`test_procedural_path` confirms the procedural tier is accessible via the same helper with a `"procedural"` tier argument.

### Missing Soul and Missing Tier Store

`test_missing_memory_returns_empty` verifies that when the soul has no memory at all, the helper returns an empty list rather than raising. `test_missing_tier_store_returns_empty` covers a partial case: the soul exists but the requested tier's store is `None` (uninitialized). Both cases are defensive — they prevent a missing-memory condition from crashing the dashboard's memory viewer.

### Entry Normalization

`test_plain_string_entries_normalised_to_content_dict` and `test_dict_entries_passed_through` test serialization normalization. Older soul formats may store entries as plain strings; newer formats use dicts with `content` and `importance`. The helper normalizes strings to `{"content": str_value}` so the API always returns a consistent shape.

## TestListSoulMemoriesEndpoint

These tests call the `list_soul_memories` endpoint function directly (not over HTTP).

### Unknown Tier Error

`test_unknown_tier_returns_error` passes an unrecognized tier name and asserts a 400-style error response. The `_ALLOWED_TIERS` frozenset is tested separately (`test_allowed_tiers_set_is_frozen`) to confirm it cannot be mutated at runtime — a mutable set could be accidentally modified by a plugin or test, changing validation behavior globally.

### Limit Clamping

`test_limit_clamped_to_upper_bound` verifies that requesting a very large limit is silently capped to the maximum allowed value. `test_limit_clamped_to_lower_bound` verifies negative or zero limits are raised to the minimum. Both tests use `monkeypatch` to inject a stub soul.

### No Soul Fall-through

`test_no_soul_returns_empty_not_error` covers deployments where no soul file has been loaded. The endpoint should return an empty list with a 200 status rather than a 404 or 500.

## Known Gaps

No TODO or FIXME markers. The tests do not cover concurrent access to the soul object or the behavior when a tier store raises an exception during iteration.