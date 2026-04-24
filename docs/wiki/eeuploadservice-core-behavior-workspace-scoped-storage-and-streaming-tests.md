---
{
  "title": "EEUploadService Core Behavior: Workspace-Scoped Storage and Streaming Tests",
  "summary": "This test suite verifies that EEUploadService correctly stores uploaded files under a workspace scope, enforces cross-workspace access boundaries, streams content back to authorized requesters, and handles bulk uploads atomically. The in-memory `_MemAdapter` stands in for real cloud storage so tests run without external dependencies.",
  "concepts": [
    "EEUploadService",
    "StorageAdapter",
    "workspace isolation",
    "bulk upload",
    "stream enforcement",
    "NotFound",
    "async generator",
    "BulkUploadResult",
    "UploadFile",
    "metadata store"
  ],
  "categories": [
    "testing",
    "file uploads",
    "enterprise edition",
    "workspace security",
    "test"
  ],
  "source_docs": [
    "f74c9047c4bea226"
  ],
  "backlinks": null,
  "word_count": 568,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `TestEEUploadService` suite covers the enterprise-edition upload service that sits on top of PocketPaw's base `StorageAdapter` protocol. Every method under test touches the workspace isolation guarantee: files uploaded to workspace `w1` must never be accessible from workspace `w2`, even if the requester holds a valid user ID.

## `_MemAdapter` — The In-Process Storage Double

```python
class _MemAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        buf = b""
        async for c in stream:
            buf += c
        self.blobs[key] = buf
        return StoredObject(key=key, size=len(buf), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)
```

`_MemAdapter` implements every method on the `StorageAdapter` ABC using a plain Python dict. This is not lazy — it forces any test that calls `svc.stream()` to exercise the real async generator path that the production S3 adapter follows. The `open` method intentionally raises `NotFound` (not `KeyError`) so the test assertions match the error surface the service exposes to callers.

## Workspace Isolation Is the Core Invariant

`test_stream_enforces_workspace` is the most important single test in the file. It uploads a file to workspace `w1`, then attempts to stream it back using workspace `w2`. The expected result is a `NotFound` exception:

```python
async def test_stream_enforces_workspace(self, store, tmp_path: Path):
    rec = await svc.upload(
        _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace="w1"
    )
    with pytest.raises(NotFound):
        await svc.stream(rec.id, requester_id="u1", workspace="w2")
```

Without this test, a scoping bug in `get_scoped` could silently allow cross-workspace data leakage. The `NotFound` response (rather than `PermissionDenied`) is deliberate: it avoids revealing that the object exists in another workspace, preventing an oracle attack.

## Happy Path: Upload → Stream → Delete

`test_stream_happy_path` verifies that bytes written during `upload` survive the full round-trip through `stream`. It reassembles chunks from the async generator and compares them byte-for-byte against the original `PNG` sentinel:

```python
got_rec, it = await svc.stream(rec.id, requester_id="u1", workspace="w1")
chunks = [c async for c in it]
assert b"".join(chunks) == PNG
```

`test_delete_owner_in_workspace` closes the lifecycle by verifying that after a soft-delete, subsequent stream attempts raise `NotFound`. This prevents deleted files from remaining accessible to anyone who already holds the record ID.

## Bulk Upload

`test_bulk_upload` uploads two files in a single call and asserts:

- Both land in `result.uploaded` with zero failures.
- Both records are queryable from the metadata store under the correct workspace.

This test exists because `upload_many` runs file storage and metadata write as separate steps per file. A regression that stores blobs but skips the metadata write (or vice versa) would break agent file-retrieval flows that depend on the workspace-scoped record.

## Test Helpers

`_upload()` wraps raw bytes in a `FastAPI.UploadFile` object backed by `io.BytesIO`. This is necessary because `EEUploadService.upload` accepts the FastAPI type directly. The `type: ignore[arg-type]` comment signals that passing a plain dict as `headers` is intentional — FastAPI's `UploadFile` accepts it internally but the type stub doesn't formally permit it.

The `PNG` constant (`b"\x89PNG\r\n\x1a\n" + b"rest"`) uses the real PNG magic bytes so any MIME-sniffing logic in the upload path encounters a recognizable file header.

## Known Gaps

No tests cover the failure path of `upload_many` where individual files fail. The `BulkUploadResult.failed` list is asserted to be empty in `test_bulk_upload` but there is no test that exercises partial failure (e.g., the adapter raising on the second file while the first succeeds). That path is exercised implicitly by the emit tests in `test_upload_emits.py`.