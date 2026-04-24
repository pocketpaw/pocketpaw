---
{
  "title": "EE Upload Service: Workspace-Scoped Pipeline over the OSS Upload Engine",
  "summary": "EEUploadService wraps the OSS UploadService to add workspace scoping, per-file access control, Mongo metadata persistence, and real-time WebSocket events for file lifecycle. It substitutes a null metadata store at the OSS layer so that all persistence flows through MongoDB rather than the JSONL flat file.",
  "concepts": [
    "EEUploadService",
    "UploadService",
    "_NullMeta",
    "workspace scoping",
    "FileReady event",
    "FileDeleted event",
    "bulk upload",
    "access control",
    "soft delete",
    "StorageAdapter",
    "Mongo persistence"
  ],
  "categories": [
    "uploads",
    "cloud EE",
    "service layer",
    "real-time events"
  ],
  "source_docs": [
    "d041770b217625fc"
  ],
  "backlinks": null,
  "word_count": 475,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/service.py` defines `EEUploadService`, the core upload business logic for the EE cloud tier. It sits between the FastAPI router and the underlying `StorageAdapter`, adding workspace isolation, access authorization, Mongo persistence, and real-time event emission on top of the OSS upload pipeline.

## Null Metadata Stub Pattern

The OSS `UploadService` requires a metadata store to persist `FileRecord` entries. In the EE tier, metadata goes to MongoDB, not the JSONL flat file that the OSS store uses. Rather than refactoring `UploadService`, `EEUploadService` passes a `_NullMeta` stub:

```python
self._oss = UploadService(adapter=adapter, meta=_NullMeta(), cfg=cfg)
```

`_NullMeta` satisfies the interface (`save`, `get`, `soft_delete`) but does nothing. All actual persistence happens in `EEUploadService`'s own methods after `_oss.upload()` succeeds. This keeps the OSS service testable without Mongo and avoids duplicating the magic-byte sniffing and MIME validation logic that lives there.

## Upload Pipeline

`upload` calls `_oss.upload()` to validate and store the bytes via the adapter, then calls `_meta.save_scoped()` to persist workspace-tagged metadata:

```python
async def upload(self, file: UploadFile, owner_id: str, chat_id: str | None, workspace: str, folder_path: str = "/") -> FileRecord:
    record = await self._oss.upload(file, owner_id=owner_id, chat_id=chat_id)
    await self._meta.save_scoped(record, workspace=workspace, folder_path=folder_path)
    await emit(FileReady(data={"workspace": workspace, "file_id": record.id}))
    return record
```

The `FileReady` event lets other parts of the system (e.g., agents, indexers) react to new uploads without polling.

## Bulk Upload

`upload_many` iterates a list of `UploadFile` objects, calling `upload` for each and collecting results into a `BulkUploadResult`. Failed individual uploads are collected as errors rather than aborting the entire batch — partial success is valid and expected for multi-file drag-and-drop scenarios.

## Access Control Gates

Two optional collaborator functions are injected at construction:

- `is_chat_member(chat_id, user_id, workspace)` — checked for read/download access on chat-attached files
- `is_workspace_admin(user_id, workspace)` — checked to allow admin-level operations beyond ownership

When `None` (e.g., in tests), these gates reduce to owner-only access. This design avoids hard-wiring database queries into the service and makes the service testable without a running group membership layer.

`_assert_can_write` and `_assert_can_read` are internal helpers that raise `NotFound` for permission denials rather than `Forbidden`. This is a deliberate security pattern: returning 404 for an unauthorized access reveals less about the system than 403, preventing enumeration of file IDs by unauthorized callers.

## Delete with Cleanup

`delete` soft-deletes the Mongo record via `_meta.soft_delete_scoped` and emits a `FileDeleted` event. It does not immediately remove the blob from the adapter. Deferred blob cleanup (garbage collection of orphaned storage keys) is a known gap.

## Known Gaps

- Blob cleanup after soft delete is not implemented. Over time, deleted files accumulate on disk or in remote storage. A background GC job that sweeps soft-deleted records older than a retention window and removes their storage keys is needed.
- `upload` is not idempotent. Retrying a failed upload after `_oss.upload()` succeeds but `save_scoped` fails would create a duplicate blob in the adapter without a corresponding metadata record.