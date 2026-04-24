---
{
  "title": "Cloud Memory Test Fixtures: mongomock-motor Beanie Isolation and MongoMemoryStore Setup",
  "summary": "This conftest provides two pytest fixtures for the cloud memory test suite: `beanie_memory_db` initializes an isolated in-memory MongoDB via `mongomock-motor` with a compatibility shim for Beanie's `list_collection_names` signature mismatch, and `store` returns a fresh `MongoMemoryStore` bound to that database. Each test gets its own uniquely-named in-memory database to prevent state leakage.",
  "concepts": [
    "conftest.py",
    "mongomock-motor",
    "Beanie",
    "MongoMemoryStore",
    "list_collection_names",
    "test isolation",
    "UUID database naming",
    "compatibility shim",
    "async fixtures",
    "in-memory MongoDB"
  ],
  "categories": [
    "Testing",
    "Cloud Memory",
    "MongoDB",
    "Test Infrastructure",
    "test"
  ],
  "source_docs": [
    "61424e30722680b7"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/memory/conftest.py` is the fixture layer for all `MongoMemoryStore` tests. It solves two problems: running MongoDB-dependent tests in CI without a live MongoDB service, and ensuring strict test isolation so that one test's writes do not affect another.

## `beanie_memory_db` Fixture

### What It Does

Creates an `AsyncMongoMockClient` (from `mongomock-motor`), names the database with a UUID suffix (`test_memory_<hex>`) to guarantee uniqueness, initializes Beanie with all cloud documents plus `MemoryFactDoc`, and yields the database.

### The `list_collection_names` Compatibility Shim

The fixture contains a subtle but important workaround:

```python
original = db.list_collection_names

async def _safe_list_collection_names(*_args, **_kwargs):
    return await original()

db.list_collection_names = _safe_list_collection_names
```

Beanie >=1.26 calls `database.list_collection_names(authorizedCollections=True, nameOnly=True)` during initialization. `mongomock-motor`'s stub does not accept these keyword arguments and raises `TypeError`. The shim intercepts the call, discards all arguments, and delegates to the no-arg version that `mongomock-motor` implements correctly.

This is a classic library version mismatch: Beanie upgraded to use a new MongoDB driver API before `mongomock-motor` had a chance to add support for it. Rather than pinning Beanie to an older version or forking `mongomock-motor`, the shim patches the incompatibility locally where it causes problems. The shim is small, isolated, and explained in comments, making it easy to remove once `mongomock-motor` adds support for the kwargs.

### UUID Database Naming

Each test invocation of `beanie_memory_db` gets a fresh database named `test_memory_<8-hex-chars>`. This is critical because Beanie maintains a module-level document registry tied to the database. If two tests share the same database name and one forgets to clean up, the second test may query stale data. UUID naming eliminates this risk entirely without requiring explicit teardown logic.

## `store` Fixture

```python
@pytest.fixture()
async def store(beanie_memory_db):
    from ee.cloud.memory.mongo_store import MongoMemoryStore
    return MongoMemoryStore()
```

A thin fixture that constructs a `MongoMemoryStore` after the Beanie initialization is complete. It depends on `beanie_memory_db` to ensure ordering, but does not receive the database object -- `MongoMemoryStore` locates its database through Beanie's internal registry, which `beanie_memory_db` has already configured.

The deferred import (`from ee.cloud.memory.mongo_store import MongoMemoryStore` inside the fixture body) is intentional: it prevents the EE module from being imported at conftest load time, which would trigger EE-only initialization code before Beanie is ready.

## Why No Teardown?

There is no explicit database cleanup after each test. `mongomock-motor` uses in-process memory -- when the test function returns, the `AsyncMongoMockClient` object goes out of scope and its data is garbage collected. The UUID database name ensures a fresh start without needing explicit drops.

## Known Gaps

The `ALL_DOCUMENTS` import from `ee.cloud.models` means this fixture initializes Beanie with all cloud document types, not just `MemoryFactDoc`. In a project with many document types, this could slow down fixture setup for tests that only need memory documents. A targeted fixture that registers only `MemoryFactDoc` would be faster, but the current approach is simpler and the overhead is negligible at the current document count.
