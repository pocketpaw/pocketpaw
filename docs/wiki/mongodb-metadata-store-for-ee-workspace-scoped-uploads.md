---
{
  "title": "MongoDB Metadata Store for EE Workspace-Scoped Uploads",
  "summary": "MongoFileStore is the persistence layer for EE upload metadata, extending the OSS file store interface with workspace isolation, folder path tracking, and bulk operations for path rewriting and soft deletion. It bridges the adapter-agnostic `FileRecord` abstraction with the EE `FileUpload` Beanie document.",
  "concepts": [
    "MongoFileStore",
    "FileRecord",
    "FileUpload",
    "workspace scoping",
    "folder path rewrite",
    "soft delete",
    "list_by_workspace",
    "Beanie",
    "upload metadata",
    "OSS compatibility"
  ],
  "categories": [
    "uploads",
    "MongoDB",
    "cloud EE",
    "file management"
  ],
  "source_docs": [
    "fe25ddc7e6cbd42d"
  ],
  "backlinks": null,
  "word_count": 423,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/mongo_store.py` provides `MongoFileStore`, the Mongo-backed metadata persistence layer for the EE uploads subsystem. It translates between the OSS `FileRecord` abstraction (from `pocketpaw.uploads.file_store`) and the EE `FileUpload` Beanie document, adding workspace scoping and folder tracking on top.

## Why a Separate EE Store

The OSS `pocketpaw.uploads` layer uses a JSONL flat-file store for metadata. The EE layer needs MongoDB because:

1. Multiple users and workspaces require shared, queryable metadata.
2. Compound indexes support efficient listing by workspace, chat, owner, and folder path.
3. Bulk operations (rewrite paths on folder rename, soft-delete folder contents) need atomic-per-document Mongo updates rather than file rewrites.

`MongoFileStore` satisfies the same interface contract as the OSS store so the service layer can swap implementations without branching.

## save_scoped: Workspace-Annotated Insert

`save_scoped` takes a `FileRecord` (OSS type) and a workspace identifier, creates a `FileUpload` document with the workspace stamped, and inserts it:

```python
async def save_scoped(self, record: FileRecord, workspace: str, *, folder_path: str = "/") -> None:
    doc = FileUpload(
        file_id=record.id,
        storage_key=record.storage_key,
        workspace=workspace,
        folder_path=folder_path or "/",
    )
    await doc.insert()
```

The `folder_path or "/"` guard ensures that a caller passing an empty string or `None` always stores a valid root path rather than an invalid empty string.

## rewrite_folder_prefix: Rename Safety

When a folder is renamed, every file under the old path needs its `folder_path` updated. `rewrite_folder_prefix` handles both the exact match (`folder_path == old_prefix`) and strict descendants (`folder_path` starts with `old_prefix + "/"`). The operation is idempotent: files already under `new_prefix` are skipped.

## soft_delete_under_prefix

Deleting a folder soft-deletes all files under its path. `soft_delete_under_prefix` sets `deleted_at = now(UTC)` on matching live files and returns the count updated. This parallels `FolderStore.soft_delete_under_prefix`, ensuring both file and folder records are consistently marked deleted.

## list_by_workspace: Unified Files Listing

Added in the 2026-04-19 update, `list_by_workspace` allows the unified files endpoint to pull chat-sourced uploads alongside local filesystem entries:

```python
async def list_by_workspace(self, workspace: str, *, limit: int = 100, chat_id: str | None = None) -> list[FileRecord]:
    ...
```

Results are capped at `limit` (default 100) to prevent unbounded list queries. Soft-deleted rows are excluded. The optional `chat_id` filter narrows results to attachments from a specific chat.

## Known Gaps

- `rewrite_folder_prefix` and `soft_delete_under_prefix` iterate and update documents individually. On folders with thousands of files, this is slow and not wrapped in a MongoDB transaction. A bulk update with `$set` would be faster and atomically consistent.
- `list_by_workspace` returns files sorted by `createdAt` descending but does not support pagination beyond a hard cap. Deep file histories require a cursor-based pagination approach.