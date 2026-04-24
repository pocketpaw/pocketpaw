---
{
  "title": "Unified Files Service Tests: Workspace Isolation, Dedup, Drive Warnings, and Limit Cap",
  "summary": "This module tests `UnifiedFilesService`, which aggregates files from multiple sources (chat uploads, Drive, local) into a single normalized list, covering workspace scope isolation, soft-delete filtering, cross-source deduplication, source-specific warnings, and the per-request limit cap. It uses `mongomock-motor` to run Beanie document tests in CI without a live MongoDB instance.",
  "concepts": [
    "UnifiedFilesService",
    "MongoFileStore",
    "workspace isolation",
    "soft delete",
    "deduplication",
    "drive warning",
    "source filtering",
    "limit cap",
    "Beanie",
    "mongomock-motor"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Unified File Listing",
    "MongoDB",
    "test"
  ],
  "source_docs": [
    "f874b2a896799472"
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

`tests/cloud/files/test_unified_list.py` covers `UnifiedFilesService` and `MongoFileStore` from `ee.cloud.files.service` and `ee.cloud.uploads.mongo_store`. The service aggregates files from multiple sources -- chat attachments stored in MongoDB, Google Drive (stubbed), and local client-side files -- into a single unified list that the frontend's Files panel displays.

## Infrastructure

The `beanie_files_db` fixture creates an isolated in-memory MongoDB via `mongomock-motor`, initializes Beanie with `FileUpload` documents, and yields the database. The fixture also patches `db.list_collection_names` to drop unknown keyword arguments that newer Beanie versions pass but `mongomock-motor` does not accept -- the same compatibility shim pattern seen in the memory test fixtures.

`_seed_upload` is a helper that creates and persists a `FileRecord` via `MongoFileStore.save_scoped`, using a unique `uuid4` ID to prevent ID collisions between tests.

## Test Breakdown

### `test_list_by_workspace_returns_rows`

Seeds two files in workspace `w1` and asserts that `MongoFileStore.list_by_workspace("w1")` returns both. This is the basic read-after-write correctness check.

### `test_list_by_workspace_skips_other_workspaces`

Seeds one file in `w-a` and one in `w-b`. Querying workspace `w-a` must return only `A-file.pdf`. This is the cross-workspace isolation guarantee: a single-tenant user who has access to multiple workspaces could inadvertently see files from a workspace they are not browsing if this query filter is missing.

### `test_list_by_workspace_soft_delete_is_hidden`

Seeds two files, then directly sets `deleted_at` on one Beanie document. Asserts that `list_by_workspace` returns only the non-deleted file. Soft deletion is used instead of hard deletion to support audit trails and potential recovery -- but soft-deleted files must be invisible to listing queries.

### `test_unified_list_includes_chat_and_drive_warning`

The unified list with `source=None` (all sources) must include chat-sourced files and emit a `"drive.not_connected"` warning when Google Drive is not connected. This warning exists so the frontend can render a "Connect Drive" nudge without polling Drive or guessing connectivity state.

```python
assert any("drive.not_connected" in w for w in warnings)
```

### `test_unified_list_chat_only_has_no_drive_warning`

When `source="chat"` is specified, only chat files are returned and no drive warning is emitted -- the user explicitly scoped to chat, so the Drive stub is not consulted.

### `test_unified_list_local_source_warns_client_only`

`source="local"` returns no files and emits a `"local.client_only"` warning. Local files live in the browser/desktop client and are not accessible from the server -- the service acknowledges the source without erroring.

### `test_dedupe_keeps_first_occurrence`

The `_dedupe` function deduplicates by filename, keeping the first occurrence. Given three rows where two share the filename `a.pdf` (from `chat` and `drive`), the deduped list contains only the `chat` version plus the unique `b.pdf`.

```python
out = _dedupe(rows)
assert [r.id for r in out] == ["a", "c"]
```

This prevents a file that exists in both Drive and chat uploads from appearing twice in the UI.

### `test_unified_list_respects_limit_cap`

Seeds six files, requests `limit=3`, and asserts exactly three are returned. The service must enforce the cap to prevent unbounded result sets from overwhelming the frontend or the database query.

## Known Gaps

There is no test for pagination -- only the first `limit` files are returned, with no cursor for subsequent pages. It is unclear whether `MongoFileStore.list_by_workspace` supports continuation tokens or always returns a fresh top-N query. Additionally, `test_unified_list_includes_chat_and_drive_warning` does not verify the `source` field of the warning, only that `"drive.not_connected"` appears as a substring.
