---
{
  "title": "Memory Backend Selection Tests: EE MongoDB Default Flip and OSS Import Isolation",
  "summary": "This module tests that the EE edition flips the default memory backend from `file` to `mongodb` at startup without overriding explicit user choices, verifies that `get_memory_manager()` returns a `MongoMemoryStore`-backed manager after the flip, and enforces that OSS package modules (`src/pocketpaw/memory/`) contain no top-level imports from `ee.*` to preserve clean open-source/enterprise boundary separation.",
  "concepts": [
    "memory backend selection",
    "MongoMemoryStore",
    "FileMemoryStore",
    "register_default_backend",
    "OSS isolation",
    "EE package boundary",
    "singleton reset",
    "source inspection",
    "POCKETPAW_MEMORY_BACKEND",
    "top-level import guard"
  ],
  "categories": [
    "Cloud Memory",
    "Testing",
    "Architecture",
    "OSS/EE Boundary",
    "test"
  ],
  "source_docs": [
    "8d06db00b8932c04"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/memory/test_backend_selection.py` addresses one of the trickiest architectural concerns in PocketPaw's dual-edition design: how the enterprise (`ee`) package changes the default memory backend to MongoDB without breaking OSS users who run the file-based backend, and without polluting the OSS package with enterprise dependencies.

## The Two-Edition Architecture

PocketPaw ships in two editions:
- **OSS**: file-based memory store (`FileMemoryStore`), zero cloud dependencies.
- **EE (Enterprise Edition)**: MongoDB-backed memory store (`MongoMemoryStore`), requires a live MongoDB instance.

The `POCKETPAW_MEMORY_BACKEND` environment variable controls which store is used. OSS defaults to `"file"`. EE wants to default to `"mongodb"` -- but the default-setting code must live in the EE package and must not run when EE is not installed.

## `TestCreateMemoryStoreMongoBranch`

### `test_mongodb_backend_returns_mongo_store`

Calls `create_memory_store(backend="mongodb")` and asserts the result is a `MongoMemoryStore` instance. This confirms the `"mongodb"` branch of `create_memory_store` is wired to the EE store class.

### `test_file_backend_unchanged`

Calls `create_memory_store(backend="file")` and asserts a `FileMemoryStore`. This confirms the EE package does not break the OSS file backend -- the two backends coexist.

## `TestEeDefaultFlip`

### `test_flip_when_env_unset`

Calls `register_default_backend()` with `POCKETPAW_MEMORY_BACKEND` absent from the environment and asserts it is set to `"mongodb"` afterward. This is the primary behavior: EE bootstrap code calls this function at startup to flip the default without touching any config file.

### `test_preserves_explicit_file_choice`

If `POCKETPAW_MEMORY_BACKEND=file` is already set, `register_default_backend()` must not overwrite it. This respects explicit user configuration -- an EE user may deliberately choose the file backend for a local development environment.

### `test_preserves_explicit_mem0_choice`

Same as above for `backend="mem0"` (a third-party memory provider integration). The function must be a no-op if any backend is already explicitly chosen.

### `test_primes_manager_singleton_with_mongo_store`

After calling `register_default_backend()`, `get_memory_manager()._store` must be a `MongoMemoryStore`. This validates end-to-end that the env var flip propagates through the singleton manager initialization.

The docstring explains a deliberate choice: `Settings.load()` is bypassed because it reads `~/.pocketpaw/config.json`, which may carry a stale `memory_backend` value from a previous session. The test forces a clean environment via `patch.dict(os.environ, ..., clear=True)` to isolate from local developer config.

## `TestOssIsolation`

### `test_no_top_level_ee_imports_in_pocketpaw_memory`

Scans every `.py` file in `src/pocketpaw/memory/` and raises `AssertionError` if any file has a zero-indentation `from ee.` or `import ee.` import. Top-level EE imports would cause import failures for OSS users who do not have the `ee` package installed.

### `test_create_memory_store_module_has_no_top_level_ee_import`

Specifically checks `pocketpaw/memory/manager.py` by source inspection. The test explains why it does not use `sys.modules` surgery: manipulating the module cache mid-test is fragile and was flagged by the security scanner. Source inspection is deterministic and immune to test order effects.

## The `_reset_memory_manager_singleton` Fixture

After each test, resets the `_manager` singleton to `None` and clears the settings cache. Without this, the manager initialized by one test (backed by MongoDB) would be reused by the next test that expects a fresh state, causing false negatives.

## Known Gaps

A comment in the source explicitly acknowledges one gap: `init_cloud_db` is not called in any test here because reinitializing Beanie mid-suite corrupts the global document registry. The contract for the full `init_cloud_db` path is deferred to `scripts/smoke_mongo_memory.py`, which runs in isolation.
