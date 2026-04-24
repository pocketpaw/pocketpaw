---
{
  "title": "Local Storage Adapter Tests: Atomic Writes, Streaming, and Error Mapping",
  "summary": "This module tests `LocalStorageAdapter`, PocketPaw's filesystem-based upload backend. It validates atomic writes (no partial files on stream errors), parent directory creation, chunk concatenation, streaming reads, and correct exception mapping from filesystem errors to typed `UploadError` subclasses.",
  "concepts": [
    "LocalStorageAdapter",
    "atomic write",
    "streaming upload",
    "NotFound",
    "StorageFailure",
    "AccessDenied",
    "async iterator",
    "parent directory creation",
    "chunk concatenation",
    "tmp_upload_root",
    "file system"
  ],
  "categories": [
    "testing",
    "uploads",
    "storage",
    "local filesystem",
    "error handling",
    "test"
  ],
  "source_docs": [
    "e7a1e8f3e2b1c350"
  ],
  "backlinks": null,
  "word_count": 432,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_local_adapter.py` tests `pocketpaw.uploads.local.LocalStorageAdapter`. The local adapter stores uploaded files on the host filesystem and is the default backend for development and single-server deployments. The tests use the `tmp_upload_root` fixture from `conftest.py` to isolate each test to a fresh directory.

## `_astream` Helper

A simple async generator that yields bytes chunks from a list. Used to simulate streaming upload bodies in tests without real HTTP.

## Write Tests (`TestLocalStorageAdapter`)

### Basic Write (`test_put_writes_bytes_and_returns_size`)

`adapter.put(key, stream, mime)` must:

1. Write the bytes to `root / key` (verified by `read_bytes()`).
2. Return a metadata object with `key`, `size` (byte count), and `mime` fields.

This round-trip confirms the adapter correctly assembles the file from the stream and records metadata.

### Parent Directory Creation (`test_put_creates_parent_dirs`)

Keys like `a/b/c/d.bin` require nested directories. The adapter must call `mkdir(parents=True, exist_ok=True)` before writing. Without this, the first upload to a new path hierarchy would fail with `FileNotFoundError`.

### Chunk Concatenation (`test_put_concatenates_chunks`)

A stream of `[b"foo", b"bar", b"baz"]` must produce a file containing `b"foobarbaz"` with `size == 9`. This verifies the adapter reads all chunks from the async iterator, not just the first.

### Atomic Write on Stream Error (`test_put_atomic_no_partial_on_stream_error`)

This is the most important test in the file. A stream that yields `b"part1"` then raises `RuntimeError("boom")` must:

1. Cause `put()` to raise `StorageFailure` (correct exception mapping).
2. Leave **no file at the target path** (no partial file).

Without atomic write semantics, a failed upload would leave a truncated file that appears to exist but contains incomplete data. Subsequent uploads of the same key would overwrite it, but a `get()` before the retry would return garbage. The test asserts atomicity by checking that the target path does not exist after the failure—implying the adapter writes to a temp file and moves it only on success, or cleans up on failure.

### Streaming Read (`test_open_streams_bytes`)

`adapter.open(key)` must return an async iterator that yields the file's bytes. The test collects chunks with `[c async for c in adapter.open(...)]` and joins them, verifying the full content is readable.

### Missing File Error (`test_open_missing_raises_not_found`)

`adapter.open("nonexistent/key")` must raise `NotFound`, not `FileNotFoundError`. This exception mapping is what allows the API router to return a 404 without catching filesystem-specific exceptions.

## Known Gaps

- No test covers `delete()` if the adapter implements it—only `put()` and `open()` are tested.
- No test verifies that `AccessDenied` is raised when the process lacks write permissions to the root directory (would require `chmod` in the test, which is platform-sensitive).
- Performance under large files (e.g., 100MB video) is not tested; the adapter may buffer the entire file in memory before writing.
