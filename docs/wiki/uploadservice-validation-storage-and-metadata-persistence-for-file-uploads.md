---
{
  "title": "UploadService: Validation, Storage, and Metadata Persistence for File Uploads",
  "summary": "UploadService is the application-layer coordinator for file uploads—it validates MIME types and sizes, delegates byte storage to the active StorageAdapter, and writes FileRecord metadata to the persistent store. It exposes both single-file and bulk-upload paths, collecting partial failures without aborting the whole batch.",
  "concepts": [
    "UploadService",
    "FileRecord",
    "BulkUploadResult",
    "FailedUpload",
    "_sniff_mime",
    "MIME sniffing",
    "ownership",
    "bulk upload",
    "metadata persistence",
    "StorageAdapter"
  ],
  "categories": [
    "uploads",
    "service layer"
  ],
  "source_docs": [
    "340fa4c87511195d"
  ],
  "backlinks": null,
  "word_count": 527,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`UploadService` sits between FastAPI route handlers and the raw storage adapter. Its role is to enforce all business rules around uploads—ownership, content-type validation, size limits, filename sanitisation—so that neither the route layer nor the storage adapter needs to know about them. The service is instantiated once at startup and shared across requests.

## MIME Sniffing vs. Declared Type

The private `_sniff_mime` function inspects the first bytes of the upload stream (`head`) and compares against the client-declared content type. This matters because browsers and HTTP clients often send `application/octet-stream` for files they don't recognise, or—more dangerously—a malicious client could declare `image/jpeg` for a file that is actually a script. By sniffing the magic bytes, the service can either correct an innocent mismatch or reject a deliberately mislabelled upload. The `fallback` parameter means that if sniffing produces no confident match, the declared type is accepted rather than blocking the upload.

## Atomic Metadata + Storage Sequence

Each upload follows a precise ordering:

1. Generate a new UUID `file_id`.
2. Derive a storage key (via `uploads/keys.py`).
3. Stream bytes to the adapter (`adapter.put`).
4. Write a `FileRecord` to the metadata store.

This order means that if step 4 fails, the blob exists in storage without a metadata entry—an orphaned blob, not a dangling metadata record. Orphaned blobs are harmless (they can be garbage-collected) whereas dangling records pointing at missing blobs would cause 404 errors for users. The asymmetry is intentional.

## Bulk Upload and Partial Failures

`upload_many` iterates files and calls `_upload_one` for each, accumulating successes in a list and failures in `FailedUpload` dataclasses. This prevents one corrupt or oversized file from blocking all others in a multi-attachment message. The `BulkUploadResult` returned to the caller separates succeeded records from failed ones so the route handler can report partial success to the frontend without losing context about which file failed and why.

## Ownership and Access Control

Every upload is tagged with `owner_id` and optionally `chat_id`. The `delete` method checks `requester_id` against the record's `owner_id` before delegating to the adapter. This is the primary access-control gate: the service refuses deletion by non-owners rather than leaving it to the storage layer, which has no concept of ownership.

## `_basename` Sanitisation

Filenames from user uploads are passed through `_basename`, which strips directory components and percent-decodes URL-encoded characters. This prevents filenames like `../../etc/cron.d/evil` from being stored, even when the storage adapter performs its own path checks—defence in depth at the service boundary.

```python
record = await service.upload(file, owner_id=user.id, chat_id=chat.id)
result = await service.upload_many(files, owner_id=user.id, chat_id=chat.id)
# result.succeeded: list[FileRecord]
# result.failed: list[FailedUpload]
```

## Known Gaps

- `_upload_one` does not enforce a maximum file size within the service itself; size limiting is expected to be handled by the FastAPI route via request body size limits or `UploadFile` constraints. If a caller bypasses the route, no size cap applies.
- `upload_many` processes files sequentially rather than concurrently. For large batches this increases total latency proportionally; a concurrent gather with a semaphore would improve throughput.
- The `_body` async iterator wraps `UploadFile.read` in chunks; if the underlying `UploadFile` is not seekable, a second call to `_body` will return empty bytes—callers must not retry a stream.
