---
{
  "title": "Uploads Test Conftest: Isolated Temporary Upload Root Fixture",
  "summary": "This conftest defines `tmp_upload_root`, a pytest fixture that provides an isolated temporary directory for each test in the uploads test suite. It prevents test pollution between file system tests by ensuring every test starts with a clean, empty upload root.",
  "concepts": [
    "conftest.py",
    "tmp_upload_root",
    "tmp_path",
    "pytest fixture",
    "test isolation",
    "LocalStorageAdapter",
    "file system tests",
    "fixture scope"
  ],
  "categories": [
    "testing",
    "uploads",
    "pytest",
    "fixtures",
    "test"
  ],
  "source_docs": [
    "a03c0f63855e115b"
  ],
  "backlinks": null,
  "word_count": 513,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/conftest.py` defines the shared test infrastructure for the entire `tests/uploads/` package in PocketPaw. It contains a single fixture, `tmp_upload_root`, which is consumed by nearly every upload-related test module. The file is minimal by design: conftest files should provide shared fixtures, not test logic.

## The `tmp_upload_root` Fixture

```python
@pytest.fixture()
def tmp_upload_root(tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    root.mkdir()
    return root
```

The fixture composes pytest's built-in `tmp_path` fixture, which provides a unique temporary directory per test function invocation (guaranteed unique by pytest's internal counter). It creates an `uploads/` subdirectory within that path and returns it.

### Why Not Use `tmp_path` Directly?

Tests could use `tmp_path` directly, but `tmp_upload_root` provides several advantages:

**Semantic clarity**: The returned path is explicitly named `uploads/`, matching the expected root structure of `LocalStorageAdapter` and `JSONLFileStore`. Tests that use `tmp_upload_root` read more naturally than tests that compute `tmp_path / "uploads"` inline.

**Single source of truth**: If the upload root path convention ever changes (e.g., to `storage/` or `files/`), updating this one fixture propagates the change to all consumers without touching individual test files.

**Explicit directory creation**: The `root.mkdir()` call ensures the directory exists before the test runs. Adapters that do not create their root directory on initialization (a common assumption) will not fail with `FileNotFoundError` on the first write.

### Isolation Guarantee

Because `tmp_path` is function-scoped, each test gets a completely fresh, unique directory on disk. This prevents:

- **State bleed between tests**: A file written by `test_put_writes_bytes_and_returns_size` never appears in `test_open_missing_raises_not_found`.
- **False positives**: A test that asserts "file does not exist" cannot accidentally pass because a previous test happened to clean up its own files.
- **False negatives**: A test that asserts "file exists" cannot accidentally fail because a different test wrote to a slightly different path.
- **Order dependency**: Tests can run in any order or in parallel without affecting each other's file system state.

pytest automatically cleans up `tmp_path` directories after the test session completes (or immediately if `--basetemp` is configured), so no manual teardown is needed.

## Scope Considerations

The fixture uses the default `function` scope (no explicit `scope=` argument). This is the correct choice for file system fixtures because:

- Shared state across tests would require careful ordering and cleanup logic.
- The overhead of creating a directory is negligible (microseconds).
- pytest's `tmp_path` cleanup handles removal automatically.

A `session`-scoped or `module`-scoped variant would be appropriate only for tests that pre-populate a large dataset and need to share it across many tests—none of the current uploads tests have this requirement.

## Downstream Consumers

The following test modules declare `tmp_upload_root` as a fixture parameter:

- `test_file_store.py` — passes it as the `path` parent for `JSONLFileStore`
- `test_local_adapter.py` — passes it as the `root` for `LocalStorageAdapter`
- `test_resolver.py` — passes it as the root for both adapter and store in `UploadResolver`
- `test_router.py` — uses `tmp_path` directly (similar but not this fixture), patching the module-level service

## Known Gaps

No `session`-scoped variant exists. Any future performance or load tests that need a pre-populated upload directory would need to define their own fixture.
