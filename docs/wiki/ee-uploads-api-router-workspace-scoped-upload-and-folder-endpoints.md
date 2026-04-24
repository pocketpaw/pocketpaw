---
{
  "title": "EE Uploads API Router: Workspace-Scoped Upload and Folder Endpoints",
  "summary": "This FastAPI router exposes all upload and folder management endpoints for the EE cloud layer, enforcing workspace scope and per-file access control on every operation. It manages the full file lifecycle — upload, download URL generation, folder CRUD, file move, and soft delete — backed by EEUploadService and FolderStore.",
  "concepts": [
    "FastAPI router",
    "EEUploadService",
    "FolderStore",
    "download URL",
    "multipart upload",
    "folder CRUD",
    "access control",
    "require_license",
    "workspace scope",
    "module singleton"
  ],
  "categories": [
    "uploads",
    "API",
    "FastAPI",
    "cloud EE"
  ],
  "source_docs": [
    "7830cf3ed3ca9527"
  ],
  "backlinks": null,
  "word_count": 472,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/router.py` defines the `/uploads` FastAPI router for the EE cloud tier. It coordinates between `EEUploadService`, `FolderStore`, and `MongoFileStore` to expose file upload, retrieval, download URL generation, and folder management.

## Module-Level Singletons

The router creates adapter, store, and folder store instances at module import time:

```python
_ROOT = Path.home() / ".pocketpaw" / "uploads"
_ADAPTER = build_adapter(_ROOT)
_META = MongoFileStore()
_FOLDERS = FolderStore()
```

This singleton pattern ensures there is one storage adapter per process. Creating a new adapter per request would open and close file handles on every call, and for future remote adapters, would create new connection pools.

## Upload Endpoint with Folder Auto-Creation

The `POST /uploads` endpoint accepts an optional `path` multipart field. If provided, it calls `ensure_chain` on `FolderStore` to materialize any missing intermediate directories before the file is stored. This means a single upload call can create an entire folder hierarchy atomically from the user's perspective.

## Download URL Alias

The 2026-04-19 update added `GET /uploads/{id}/download-url` as a named alias for the existing `/grant` endpoint. The alias adds an `expires_at` timestamp and a `filename` field to the response, giving the frontend a standard way to offer a Save As dialog with the original filename as the default.

## Access Control Architecture

Access to individual files is checked via two optional collaborators passed into `EEUploadService`:

- `_is_chat_member(chat_id, user_id, workspace)` — returns `True` if the user is a member of the chat group that the file was attached to
- `_is_workspace_admin(user_id, workspace)` — returns `True` if the user is an owner or admin

For chat-attached files, membership in the chat group grants read access. For My Files uploads, only the owner and workspace admins can read or delete. This tiered model prevents chat attachment access from being workspace-wide.

## Folder Endpoints

The router exposes four folder management endpoints added 2026-04-21:

- `POST /uploads/folders` — create a folder, auto-creating the chain if needed
- `PATCH /uploads/folders/{id}` — rename a folder; triggers `rewrite_path_prefix` on both files and folders under the old path
- `DELETE /uploads/folders/{id}` — soft-delete a folder and all its contents
- `PATCH /uploads/{file_id}` — move a file to a different folder path

## License Gate

All endpoints go through `require_license` from `ee.cloud.license`. This ensures that upload features are unavailable on installations without a valid EE license, without each handler needing to repeat the check.

## Known Gaps

- Folder rename calls `rewrite_path_prefix` on both files and folders, but these two operations are not wrapped in a MongoDB transaction. A process crash between the two rewrites would leave files with stale paths under the old folder name while the folder record reflects the new name.
- The `_is_chat_member` helper is defined inline in the router module rather than delegated to a service. Business logic in the router layer makes it harder to unit-test the access control rules independently.