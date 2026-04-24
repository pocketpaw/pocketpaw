---
{
  "title": "Upload and Folder Document Models for the EE Storage Layer",
  "summary": "This module defines the two Beanie document models underpinning the EE uploads subsystem: `FileUpload` for adapter-backed file metadata and `FileFolder` for the virtual folder tree. Both documents are workspace-scoped with compound indexes designed for the access patterns of the uploads and files listing endpoints.",
  "concepts": [
    "FileUpload",
    "FileFolder",
    "Beanie Document",
    "TimestampedDocument",
    "storage_key",
    "soft delete",
    "compound index",
    "folder_path",
    "chat attachments",
    "My Files"
  ],
  "categories": [
    "uploads",
    "MongoDB",
    "data models",
    "cloud EE"
  ],
  "source_docs": [
    "d5d29e2a81307142"
  ],
  "backlinks": null,
  "word_count": 431,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/models.py` provides the MongoDB document schemas for the EE upload layer. The two models — `FileUpload` and `FileFolder` — separate concerns cleanly: one tracks file metadata, the other tracks folder structure.

## FileUpload: Metadata, Not Bytes

`FileUpload` stores metadata about an uploaded file. The actual bytes live in a `StorageAdapter` (local disk or a future remote backend), addressed by `storage_key`. This separation means the Mongo document is small and queryable while binary data stays in the appropriate storage backend.

`FileUpload` is intentionally distinct from `ee.cloud.models.file.FileObj`, which is used for pre-signed URL storage. `FileUpload` is the adapter-backed path for chat attachments and the My Files feature, with workspace scoping and soft-delete capabilities that `FileObj` does not have.

## Key Fields

- `file_id` — a unique identifier indexed for fast lookup by ID, separate from MongoDB's `_id`
- `storage_key` — the adapter-relative path to the blob; only the adapter knows how to resolve this to bytes
- `folder_path` — the virtual folder path for the My Files mount; defaults to `"/"` and `None` on legacy rows is treated as root
- `deleted_at` — soft-delete timestamp; `None` means the file is live

## Compound Indexes

Three compound indexes are declared on `FileUpload`:

```python
indexes = [
    [("workspace", 1), ("chat_id", 1), ("createdAt", -1)],
    [("workspace", 1), ("owner", 1), ("createdAt", -1)],
    [("workspace", 1), ("folder_path", 1), ("deleted_at", 1)],
]
```

The first two support the two primary list endpoints: chat attachment history and user file history, both sorted newest-first. The third supports folder browsing — filtering by workspace, folder path, and live status in one index scan.

## FileFolder: Virtual Directory Nodes

`FileFolder` represents a single directory node in the My Files virtual filesystem. It stores a normalized absolute `path` (e.g., `/reports/2026`) and a `name` (the last segment). The redundant `name` field avoids computing it on every listing query.

Folders do not exist for other providers in this release. A flat listing is used for knowledge-base, chat, and drive providers; only the uploads provider has the full nested folder tree.

## Soft Delete on Folders

`FileFolder` also has a `deleted_at` field matching the pattern on `FileUpload`. Soft deletes on both documents ensure that bulk operations (delete folder and contents) can be recorded without permanent data loss.

## Known Gaps

- `folder_path` on legacy `FileUpload` rows is `None`, not `"/"`. Code that queries by `folder_path == "/"` will miss legacy rows unless the query also includes `folder_path == None`. The migration from `None` to `"/"` may not be complete.
- There is no index on `FileUpload.deleted_at` alone, making bulk cleanup queries across workspaces a full collection scan.