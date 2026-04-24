---
{
  "title": "Upload Service Tests: Validation, Bulk Handling, and Streaming",
  "summary": "This test suite exercises the `UploadService` layer in PocketPaw's file upload subsystem, covering single and bulk file ingestion, magic-byte MIME sniffing, path sanitisation, and ownership-gated streaming and deletion. It uses an in-memory `_FakeAdapter` to isolate tests from real storage backends while still exercising the full service contract.",
  "concepts": [
    "UploadService",
    "StorageAdapter",
    "magic-byte sniffing",
    "MIME allowlist",
    "path traversal sanitisation",
    "bulk upload",
    "partial failure handling",
    "ownership access control",
    "idempotent delete",
    "JSONLFileStore",
    "UploadSettings",
    "async streaming",
    "EmptyFile",
    "TooLarge",
    "UnsupportedMime"
  ],
  "categories": [
    "file uploads",
    "testing",
    "security",
    "storage",
    "test"
  ],
  "source_docs": [
    "7acd1d4bf746cda9"
  ],
  "backlinks": null,
  "word_count": 634,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `UploadService` is the business-logic core of PocketPaw's upload subsystem. It sits between the HTTP layer and a pluggable `StorageAdapter`, enforcing constraints like file size limits, MIME allowlists, and ownership before delegating persistence. This test file locks down all critical paths through that contract.

## Fake Adapter Pattern

`_FakeAdapter` implements `StorageAdapter` using a plain in-memory `dict[str, bytes]`. This is intentional: tests that need to verify service-layer logic — what gets stored, when errors are raised, how metadata is recorded — should not depend on real disk or object-storage APIs. The fake's `open` method uses `async for` to match the real streaming interface, which means tests also exercise async generator consumption paths.

```python
class _FakeAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        data = b""
        async for chunk in stream:
            data += chunk
        self.blobs[key] = data
        return StoredObject(key=key, size=len(data), mime=mime)
```

## Single Upload Validations

`TestUploadServiceSingle` covers the rejection cases that prevent unsafe or malformed files from entering storage:

- **Oversize rejection** (`TooLarge`): Prevents unbounded disk consumption. The service checks byte count against a configurable cap before writing anything.
- **MIME allowlist** (`UnsupportedMime`): SVG and other dangerous types are blocked. SVG can carry embedded JavaScript, making it a cross-site scripting vector if served back to browsers.
- **Empty file rejection** (`EmptyFile`): A zero-byte file is never a valid upload and usually indicates a client bug or a truncated multipart body.
- **Magic-byte sniffing overrides declared MIME**: A client that sends `image/jpeg` for a PNG file gets the corrected MIME stored. This prevents MIME confusion when files are later served — a mismatch between the declared and actual type can cause browsers to mishandle downloads.
- **Path separator sanitisation**: A filename like `../evil.png` is stripped to `evil.png`. Without this, a storage key built from the raw filename could overwrite files outside the intended directory on adapters that map keys to filesystem paths.

## Bulk Upload

`TestUploadServiceBulk` validates the `upload_many` method, which processes a list of files and collects individual outcomes:

- **Partial failure**: If one file exceeds the size limit and another has an unsupported MIME, both are reported in `result.failed` while the valid file lands in `result.uploaded`. This matters because the caller needs to know exactly which files failed and why, without rolling back the successful ones.
- **Empty batch raises**: Passing an empty list is a programmer error, not a user error, so the service raises `ValueError` immediately.
- **Batch size cap**: Sending more files than `max_files_per_batch` raises before any processing begins, capping the attack surface for resource exhaustion.

## Streaming and Deletion

`TestStreamAndDelete` exercises the access-control layer over retrieval and removal:

- **Owner-only streaming**: `stream(file_id, requester_id)` raises `NotFound` for a requester who is not the owner. Returning `NotFound` (rather than `403 Forbidden`) avoids leaking whether a file with that ID even exists to non-owners — an information-hiding pattern common in multi-tenant APIs.
- **Missing file raises NotFound**: Asking for a file ID that was never stored returns `NotFound`, matching the same error type as the wrong-owner case.
- **Delete is idempotent for owner**: After a successful delete, a second delete by the same owner raises `NotFound`. This means delete is idempotent in result — the file is gone either way — but the second call does communicate that there was nothing left to delete.
- **Non-owner delete raises NotFound**: Same reasoning as non-owner streaming; the error type masks ownership information.

## Known Gaps

No `TODO`, `FIXME`, or `HACK` markers are present in this file. The batch upload tests do not exercise concurrency — two simultaneous `upload_many` calls against shared state are not covered. The fake adapter does not simulate partial write failures (e.g., a `StorageAdapter` that succeeds on `put` but fails on `exists`), so adapter-level error paths in the service are untested here.