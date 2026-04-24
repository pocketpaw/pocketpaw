---
{
  "title": "Cloud Chat Test Fixtures: In-Memory MongoDB via mongomock-motor",
  "summary": "The `beanie_memory_db` fixture initialises Beanie against an isolated in-memory MongoDB database for each test using `mongomock-motor`, enabling the full cloud chat test suite to run in CI without a real MongoDB service. It includes a compatibility shim that silences unsupported keyword arguments in mongomock-motor's `list_collection_names` stub.",
  "concepts": [
    "beanie_memory_db",
    "mongomock-motor",
    "Beanie",
    "AsyncMongoMockClient",
    "list_collection_names",
    "compatibility shim",
    "pytest fixture",
    "in-memory MongoDB",
    "ALL_DOCUMENTS",
    "CI testing"
  ],
  "categories": [
    "testing",
    "database",
    "fixtures",
    "test"
  ],
  "source_docs": [
    "4058f604037731f8"
  ],
  "backlinks": null,
  "word_count": 506,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Cloud chat tests exercise code that writes and reads MongoDB documents through Beanie ODM. Running these tests against a real MongoDB instance in CI adds infrastructure cost, flakiness risk (network timeouts, state bleed between tests), and configuration complexity. `conftest.py` solves this by providing a `beanie_memory_db` fixture that substitutes an in-memory `mongomock-motor` client, giving each test a clean isolated database with zero external dependencies.

## `beanie_memory_db` Fixture

The fixture is declared as an `async` pytest fixture (compatible with `pytest-asyncio`). Its steps:

1. Generate a unique database name using `uuid.uuid4().hex[:8]` to prevent state bleed between concurrent test workers.
2. Create an `AsyncMongoMockClient` from `mongomock-motor`.
3. Apply the compatibility shim (see below).
4. Call `init_beanie(database=db, document_models=ALL_DOCUMENTS)` to register all document models.
5. Yield — the test runs here.
6. Implicit cleanup: the in-memory client is garbage-collected after the test.

Because each test gets a new UUID-named database on a fresh mock client, there is no shared mutable state between tests, eliminating the need for teardown or collection-level reset logic.

## The `list_collection_names` Compatibility Shim

Beanie >= 1.26 calls `database.list_collection_names(authorizedCollections=True, nameOnly=True)` during initialisation. The `mongomock-motor` stub does not accept those keyword arguments and raises `TypeError`. The shim wraps the original method in `_safe_list_collection_names`, which accepts and discards all positional and keyword arguments before delegating to the no-argument form that `mongomock-motor` does support:

```python
async def _safe_list_collection_names(*_args, **_kwargs):
    # mongomock-motor doesn't honour authorizedCollections / nameOnly;
    # the no-arg call returns the same list we need for Beanie init.
    return await original()

db.list_collection_names = _safe_list_collection_names
```

This is a targeted monkey-patch applied only within the fixture scope. It keeps the test suite compatible with newer Beanie versions without waiting for `mongomock-motor` to add the missing kwargs support—a pragmatic workaround clearly documented in the comment.

## `ALL_DOCUMENTS` and `MemoryFactDoc`

`init_beanie` requires the full list of Beanie document models to initialise their collections. The fixture imports `ALL_DOCUMENTS` from `ee.cloud.models`, which is the authoritative registry of all EE cloud document classes. `MemoryFactDoc` is also imported separately, likely because it is used directly in specific chat tests in this sub-package that verify memory persistence behaviour.

## Scope and Sharing

The fixture uses `@pytest.fixture()` (function scope, the default), giving each test its own clean database. If the cost of `init_beanie` initialisation becomes significant as the model count grows, upgrading to session or module scope would share the initialisation—but would require careful test isolation discipline to avoid state bleed across tests.

## Known Gaps

- The shim is a monkey-patch on a live database object. If `mongomock-motor` adds proper kwargs support in a future version, the shim becomes dead code that silently discards arguments. No assertion guards against this drift.
- `ALL_DOCUMENTS` is imported from `ee.cloud.models`. If new document models are added to EE but not to `ALL_DOCUMENTS`, they will be absent from the test database, causing tests to fail with confusing errors rather than a clear missing-model message.
- There is no explicit fixture for test data insertion. Tests that need pre-populated documents must insert them inline, which can become verbose for complex multi-document scenarios.
