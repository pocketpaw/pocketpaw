---
{
  "title": "StorageAdapter Protocol: The Backend-Agnostic Interface for File Persistence",
  "summary": "The `StorageAdapter` protocol defines the minimal async interface that all storage backends (local disk, S3, GCS) must satisfy, keeping the upload pipeline decoupled from any specific storage technology. The companion `StoredObject` dataclass represents the canonical result of a successful write operation.",
  "concepts": [
    "StorageAdapter",
    "StoredObject",
    "Protocol",
    "put",
    "open",
    "delete",
    "exists",
    "local_path",
    "presigned_get",
    "async iterator",
    "structural subtyping",
    "storage backends"
  ],
  "categories": [
    "uploads",
    "storage",
    "architecture",
    "protocols"
  ],
  "source_docs": [
    "d44ec56f8e65d459"
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

PocketPaw's upload pipeline must work in multiple deployment contexts: local development with on-disk storage, cloud deployments with S3, and potentially other object stores. `adapter.py` defines the contract that makes backends swappable -- any class that implements the four core methods satisfies `StorageAdapter`, and the rest of the upload system never needs to know which backend it is talking to.

## StoredObject

`StoredObject` is a frozen dataclass returned by every `put()` call:

```python
@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    mime: str
```

Being frozen (immutable) makes it safe to pass around, cache, or use as a dict key. It carries exactly the information callers need after a write: the canonical key to address the blob, its size (for quota tracking), and the confirmed MIME type.

## The Four Core Methods

**`put(key, stream, mime) -> StoredObject`** -- writes a byte stream at the given key, returns a `StoredObject`. The stream is `AsyncIterator[bytes]` to support chunked uploads from HTTP requests without buffering the entire file in memory.

**`open(key) -> AsyncIterator[bytes]`** -- yields the stored blob in chunks. Note the signature: it is not `async def open` -- implementations are async generator functions, so the call `adapter.open(key)` returns an async iterator synchronously, and iteration is async. This distinction is documented explicitly in the protocol docstring to prevent implementors from using `await adapter.open(key)`.

**`delete(key) -> None`** -- removes a blob if present. The protocol specifies idempotency: calling `delete` on a non-existent key should not raise.

**`exists(key) -> bool`** -- returns whether a key is currently stored.

## Two Extended Methods

**`local_path(key) -> Path | None`** -- a local adapter can return an absolute filesystem path, allowing agent tools (like the `Read` tool) to access uploaded files directly without streaming through HTTP. Remote adapters return `None`.

**`presigned_get(key, ttl_seconds) -> str | None`** -- for adapters that support presigned URLs (S3, GCS), returns a time-limited public URL the browser can fetch without an `Authorization` header. Local adapters return `None`.

## Structural Subtyping via Protocol

`StorageAdapter` extends `typing.Protocol`, meaning any class with the right method signatures satisfies the type without explicitly inheriting from `StorageAdapter`. Third-party adapters can satisfy the protocol without depending on `pocketpaw.uploads`.

## Known Gaps

The protocol specifies `delete()` as idempotent but does not specify what exception to raise when `open()` is called on a missing key. The module-level comment mentions a `NotFound` error from `pocketpaw.uploads.errors`, but this is not enforced in the protocol definition -- each adapter implements its own raise behavior. A type annotation for the expected exception type would make this contract explicit.