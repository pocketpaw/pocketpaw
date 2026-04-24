---
{
  "title": "LocalStorageAdapter: Atomic Local-Disk Blob Storage",
  "summary": "LocalStorageAdapter provides a local-filesystem implementation of the StorageAdapter protocol, using aiofiles for non-blocking I/O and an atomic write pattern to prevent partial-file corruption. It enforces strict path isolation so that no key—however crafted—can read or write outside the designated root directory.",
  "concepts": [
    "LocalStorageAdapter",
    "StorageAdapter",
    "aiofiles",
    "atomic write",
    "path traversal",
    "AccessDenied",
    "NotFound",
    "StorageFailure",
    "chunked streaming",
    "upload backend"
  ],
  "categories": [
    "uploads",
    "storage"
  ],
  "source_docs": [
    "cd2fe8bd918f3268"
  ],
  "backlinks": null,
  "word_count": 525,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`LocalStorageAdapter` is the default, zero-dependency storage backend for PocketPaw's upload system. It stores uploaded blobs as plain files on a local disk, making development and single-host deployments simple without requiring cloud credentials. The class inherits from `StorageAdapter` and implements the same async interface as the S3 adapter, so callers are backend-agnostic.

## Why Atomic Writes Matter

The `put` method writes data to a `.tmp` sibling file first, then calls `os.rename` to atomically replace the final target. This two-step pattern exists because file systems guarantee that `rename` is atomic on POSIX: either the old file survives, or the new file appears fully—there is no window where a reader sees a partially written blob. Without this guard, a crash or power interruption during a large upload would leave a truncated file that the system would treat as valid on the next read, potentially causing silent data corruption or agent tool failures.

## Path Traversal Prevention

The `_resolve` method normalises every key through `Path.resolve()` before checking that the result is still inside `root`. This blocks path traversal attacks like `../../etc/passwd`. The check raises `AccessDenied` rather than a generic error so that the API layer can translate it cleanly to HTTP 403 without leaking internal path information. This is especially important because upload keys are partially derived from user input (file names, IDs), making injection attempts plausible even in single-user deployments.

## Chunked Streaming with aiofiles

Reading blobs back is done in chunks of 64 KB via an `AsyncIterator[bytes]`. The chunk size is deliberately modest: chat-sized images and documents are rarely multi-gigabyte, so 64 KB balances memory pressure against syscall overhead. Using `aiofiles` pushes the blocking `read()` syscall off the asyncio event loop onto a thread-pool executor, which keeps the HTTP server responsive during large downloads.

## Error Surface

Three custom exceptions gate the public interface:

- `AccessDenied` — key escapes the root (path traversal) or is otherwise forbidden
- `NotFound` — key does not exist on disk (`open` and `delete` paths)
- `StorageFailure` — any unexpected I/O error (permission denied by OS, disk full, etc.) is caught and re-raised as `StorageFailure` so callers get a single predictable exception hierarchy

Callers never see raw `OSError` or `FileNotFoundError`, which means the router layer and tests can match against the adapter's own exception types regardless of which backend is active.

## Integration

`LocalStorageAdapter` is constructed with a `root: Path` at application startup (typically `~/.pocketpaw/uploads/`). It does not create the root directory itself—that responsibility lives in the service or CLI entrypoint, which allows the adapter to be constructed against a pre-existing directory without side effects in tests.

```python
adapter = LocalStorageAdapter(root=Path("~/.pocketpaw/uploads").expanduser())
record = await adapter.put(key, stream, mime="image/jpeg")
```

## Known Gaps

- `delete` does not return a boolean indicating whether the file existed; callers that need idempotent deletes must call `exists` first or catch `NotFound`.
- No quota enforcement: the adapter will write until the disk is full. The `UploadService` layer controls size limits before data reaches this adapter, but nothing prevents direct usage without a size cap.
- No directory cleanup: deleting all blobs under a subdirectory leaves empty directories on disk, which accumulate over time in long-lived deployments.
