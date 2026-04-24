---
{
  "title": "SoulManager Lifecycle Tests: Birth, Persistence, Import, and Concurrency",
  "summary": "Comprehensive tests for `SoulManager`, the singleton that manages a PocketPaw soul's full lifecycle — from birth through autosave, import, reload, and shutdown. Covers tool caching, dirty tracking, concurrent observe serialization, corrupt file fallback, and biorhythm configuration.",
  "concepts": [
    "SoulManager",
    "soul lifecycle",
    "dirty tracking",
    "tool caching",
    "concurrent observe",
    "autosave",
    "corrupt file recovery",
    "import formats",
    "biorhythm",
    "external change detection"
  ],
  "categories": [
    "testing",
    "soul management",
    "persistence",
    "test"
  ],
  "source_docs": [
    "177ad0a9c4ce6ca8"
  ],
  "backlinks": null,
  "word_count": 504,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`SoulManager` is PocketPaw's interface to the soul-protocol SDK. It owns the soul's lifecycle: creating (birthing) a new soul, reawakening a saved one, accumulating observations, exposing soul tools to the agent, and persisting state. Because it is a module-level singleton, its test suite must be especially careful about state isolation and concurrency.

## Birth and Reawaken

`test_initialize_births_new_soul` confirms that a fresh `SoulManager` creates a new soul when no file exists. `test_save_and_reawaken` extends this: save the soul to disk, then re-initialise from the same path and confirm the soul's identity is preserved. This is the fundamental persistence contract.

## Core Tool Exposure

`test_get_tools_exposes_core_soul_tools` performs a subset-check: `get_tools()` must include at least the six tools PocketPaw depends on. The comment notes these may be renamed in future soul-protocol versions, which is why a subset-check (not an exact-match) is used — it is resilient to additions but will catch regressions if a core tool is removed.

## Corrupt File Fallback

`test_corrupt_soul_file_falls_back_to_birth` writes an invalid file to the soul path and confirms the manager births a fresh soul rather than crashing. This protects against the real-world scenario where a soul file is partially written due to a crash or disk error.

## Concurrency Serialization

`test_concurrent_observe_is_serialized` fires multiple `observe()` calls concurrently and confirms no exception is raised and the soul reaches a consistent state. Soul observation mutates the in-memory soul object; without serialization (an asyncio lock), concurrent mutations could corrupt the soul's memory structures.

## Shutdown

`test_shutdown_saves_and_stops_autosave` verifies that `shutdown()` writes the current soul state to disk and cancels the autosave background task. A missing save on shutdown means the last session's memories are lost; a leaked autosave task means the process cannot exit cleanly.

## Import Formats

Four import tests cover `soul_file`, `yaml`, and `json` config formats, plus an update-in-place test that confirms the bootstrap provider is refreshed after import. Two negative tests confirm that unsupported formats raise an error and missing files raise an error. This breadth of format support allows users to migrate souls between tools and environments.

## Reload and External Change Detection

`test_reload_from_disk` and `test_reload_returns_false_when_no_file` validate the hot-reload path. `test_external_file_change_detection` confirms the manager can detect when the soul file has been modified externally (e.g., by another process or `soul` CLI), enabling eventual consistency without continuous polling.

## Tool Caching and Invalidation

`test_tools_are_cached` confirms repeated `get_tools()` calls return the same object. `test_tools_cache_invalidated_on_import` confirms the cache is cleared after an import, forcing the next call to re-derive the tool list from the updated soul.

## Dirty Tracking and Evaluate

`test_dirty_tracking` validates the dirty flag lifecycle: clean after init, dirty after an observation, clean after save. `test_evaluate_returns_none_when_unsupported` guards against calling `evaluate()` on soul versions that don't support it.

## Biorhythm Settings

`test_biorhythm_settings_passed` confirms that biorhythm configuration (energy decay curves, sleep thresholds) is forwarded to the soul on creation — critical for the soul's personality evolution model.

## Known Gaps

None visible from the AST. The test count (20+ methods) suggests thorough coverage.

```python
@pytest.fixture
def soul_settings(tmp_path):
    # Returns SoulSettings pointed at a temp directory
    ...
```
