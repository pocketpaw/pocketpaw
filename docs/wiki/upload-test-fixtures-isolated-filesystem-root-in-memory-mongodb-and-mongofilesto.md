---
{
  "title": "Upload Test Fixtures: Isolated Filesystem Root, In-Memory MongoDB, and MongoFileStore",
  "summary": "Shared pytest fixtures for the uploads test suite providing a per-test isolated filesystem root, an in-memory MongoDB instance via `mongomock-motor`, and a ready-to-use `MongoFileStore`. The `beanie_upload_db` fixture includes a workaround for a `mongomock-motor` incompatibility with Beanie's `list_collection_names` call.",
  "concepts": [
    "pytest fixtures",
    "mongomock-motor",
    "Beanie",
    "MongoFileStore",
    "tmp_path",
    "in-memory MongoDB",
    "FileUpload",
    "FileFolder",
    "fixture isolation",
    "circular imports",
    "workaround"
  ],
  "categories": [
    "testing",
    "test fixtures",
    "uploads",
    "MongoDB",
    "test"
  ],
  "source_docs": [
    "7d51eabb706bde3e"
  ],
  "backlinks": null,
  "word_count": 356,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/uploads/conftest.py` provides three fixtures shared across all upload test modules: `tmp_upload_root`, `beanie_upload_db`, and `store`. These fixtures eliminate boilerplate in each test file and ensure complete isolation between test runs.

## tmp_upload_root

```python
@pytest.fixture()
def tmp_upload_root(tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    root.mkdir()
    return root
```

pytest's built-in `tmp_path` fixture provides a unique temporary directory per test. `tmp_upload_root` adds a named `uploads/` subdirectory so test paths look like production paths rather than bare temp dirs. This isolation means a test that writes files cannot affect another test's storage root, preventing flaky cross-test pollution.

## beanie_upload_db

This fixture spins up an in-memory MongoDB using `mongomock-motor` (an async-compatible MongoDB mock) and initializes Beanie with the `FileUpload` and `FileFolder` document models. Key implementation details:

**Unique database names** — each invocation generates a random `db_name` (`test_uploads_{uuid4}[:8]`). This is belt-and-suspenders isolation: even if two tests somehow share the same mock client, they operate on separate databases.

**`list_collection_names` workaround** — `mongomock-motor`'s implementation of `list_collection_names` does not match the signature that Beanie's `init_beanie` expects. The fixture patches the method with `_safe`, which accepts and discards any positional/keyword arguments before delegating to the original. Without this patch, `init_beanie` raises a `TypeError` at startup. This is a known `mongomock-motor` compatibility gap.

**Deferred imports** — `FileUpload` and `FileFolder` are imported *after* the mock client is set up to avoid circular import issues. Beanie's module-level initialization hooks can fire on import, and importing before the mock client is ready would cause Beanie to connect to a real (or absent) MongoDB instance.

## store

```python
@pytest.fixture()
async def store(beanie_upload_db):
    from ee.cloud.uploads.mongo_store import MongoFileStore
    return MongoFileStore()
```

This fixture depends on `beanie_upload_db` to ensure Beanie is initialized before `MongoFileStore` is constructed. `MongoFileStore` uses Beanie's `FileUpload` document class internally; if Beanie is not initialized, any query would raise.

## Known Gaps

The `beanie_upload_db` fixture is function-scoped (the default), meaning Beanie is re-initialized for every test. This is safe but slow for large test suites. A session-scoped variant with manual collection cleanup between tests would be faster but more complex. There are no fixtures for seeding known test data, so each test must set up its own state.
