---
{
  "title": "MongoFileStore Unit Tests: Scoped Save/Get, Cross-Workspace Isolation, and Soft Delete",
  "summary": "Unit tests for `MongoFileStore`'s workspace-scoped CRUD operations, validating that files are stored and retrieved correctly within a workspace, are invisible across workspace boundaries, disappear after soft deletion, and return `None` (not raise) for missing IDs.",
  "concepts": [
    "MongoFileStore",
    "FileRecord",
    "save_scoped",
    "get_scoped",
    "soft_delete_scoped",
    "workspace isolation",
    "multi-tenant",
    "Beanie",
    "soft deletion",
    "None return"
  ],
  "categories": [
    "testing",
    "uploads",
    "MongoDB",
    "data store",
    "test"
  ],
  "source_docs": [
    "4b7d7c67714c7ba5"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MongoFileStore` is PocketPaw's Beanie-backed metadata store for uploaded files in the EE tier. Every operation is workspace-scoped: files stored under workspace `w1` are never visible to queries made in workspace `w2`. This test class (`TestMongoFileStore`) isolates each invariant to confirm the store's contract without touching the HTTP layer or the storage adapter.

## Test Data Factory

`_record(**overrides)` produces a `FileRecord` with sane defaults:

- `id="f1"`, `storage_key="chat/202604/aaa.png"`, `filename="cat.png"`
- `mime="image/png"`, `size=1`, `owner_id="u1"`, `chat_id="c1"`
- `created=datetime.now(UTC)`

Callers override individual fields with keyword arguments. This factory pattern prevents each test from repeating the full constructor call and makes the relevant variation (the one overridden field) visually obvious when reading the test. It also prevents accidental coupling between tests that would occur if they shared a mutable fixture object.

## Save and Retrieve (Happy Path)

`test_save_then_get` calls `save_scoped(record, workspace="w1")` then `get_scoped("f1", workspace="w1")` and asserts the returned record is not `None` and has the correct `filename`. This is the baseline: if the store cannot round-trip a file record, none of the other tests are meaningful. It also confirms that `save_scoped` commits immediately (Beanie does not buffer writes by default in this configuration).

## Cross-Workspace Isolation

`test_cross_workspace_get_returns_none` saves a record to `workspace="w1"` but queries with `workspace="w2"`. The result must be `None`. This is the core multi-tenant invariant: workspace scoping is enforced at the database query level (the MongoDB filter includes a `workspace` predicate), not solely at the application layer. If the query accidentally omitted the workspace filter, any user who guessed another user's file ID could retrieve that file's metadata — a tenant isolation breach.

## Soft Delete Hides Records

`test_soft_delete_hides` saves a record, calls `soft_delete_scoped("f1", workspace="w1")`, then queries again with `get_scoped`. The query returns `None`. PocketPaw's upload system uses soft deletion — records are marked as deleted (typically via a `deleted_at` timestamp) but remain in the database for potential audit or recovery purposes. The `get_scoped` method filters out soft-deleted records, so callers never need to check the deletion flag themselves.

## Missing ID Returns None, Not an Exception

`test_get_missing_returns_none` queries for ID `"nope"` against an empty workspace and asserts the result is `None`. This design choice — returning `None` rather than raising a database exception — is important for the service layer. The service can do:

```python
record = await store.get_scoped(file_id, workspace=workspace)
if record is None:
    raise NotFound()
```

This pattern keeps the error-handling path explicit and avoids try/except blocks around every store call. If the store raised instead, callers would need to know which database exception to catch, creating a leaky abstraction.

## Known Gaps

There are no tests for bulk save, listing all files by workspace, or cursor-based pagination. There is no test verifying `soft_delete_scoped` on a non-existent ID (expected behavior is a no-op, but this is unverified). There are also no tests confirming that soft-deleted records can be retrieved by an admin-level query that intentionally includes deleted records.
