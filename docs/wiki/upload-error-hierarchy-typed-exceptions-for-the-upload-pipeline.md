---
{
  "title": "Upload Error Hierarchy: Typed Exceptions for the Upload Pipeline",
  "summary": "The `uploads/errors.py` module defines a typed exception hierarchy rooted at `UploadError`, with specific subclasses for each failure mode (file too large, unsupported MIME, empty file, not found, access denied, storage failure). Machine-readable `code` class attributes on each exception type enable API layers to translate exceptions into structured error responses without string matching.",
  "concepts": [
    "UploadError",
    "TooLarge",
    "UnsupportedMime",
    "EmptyFile",
    "NotFound",
    "AccessDenied",
    "StorageFailure",
    "exception hierarchy",
    "error codes",
    "typed exceptions",
    "API error handling"
  ],
  "categories": [
    "uploads",
    "error handling",
    "architecture"
  ],
  "source_docs": [
    "c8f9c8be2e68a3c0"
  ],
  "backlinks": null,
  "word_count": 407,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The upload pipeline can fail for several distinct reasons, and callers need to distinguish between them to return appropriate HTTP status codes and user-facing messages. `errors.py` establishes a single-inheritance exception hierarchy where every upload failure is an `UploadError`, but each specific scenario has its own type with a predictable `code` string.

## The Hierarchy

```
UploadError (base)
├── TooLarge          code="too_large"
├── UnsupportedMime   code="unsupported_mime"
├── EmptyFile         code="empty"
├── NotFound          code="not_found"
├── AccessDenied      code="access_denied"
└── StorageFailure    code="storage_error"
```

Each leaf class adds only its `code` class attribute and, for classes with a natural default message, a custom `__init__` that provides that default.

## Machine-Readable Codes

The `code` attribute is a class-level string that API handlers can read without pattern matching on the exception class name:

```python
try:
    stored = await pipeline.ingest(...)
except UploadError as exc:
    return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=...)
```

This pattern is more robust than `isinstance` checks across module boundaries and more maintainable than matching on `str(exc)`. It also enables consistent error codes in the API response body regardless of which layer in the pipeline raised the exception.

## Default Messages

Four exceptions provide default messages in their `__init__`:

- `EmptyFile("file is empty")` -- catches zero-byte uploads before they reach storage.
- `NotFound("not found")` -- raised by storage adapters when `open()` or `exists()` reveals a missing key.
- `AccessDenied("access denied")` -- raised when a user attempts to access a file they do not own.
- `StorageFailure` -- no default message; callers are expected to include backend-specific error context when raising.

## Why a Custom Hierarchy Rather Than Generic Exceptions

Python's built-in exceptions (`ValueError`, `FileNotFoundError`) do not carry the `code` attribute needed for API responses. Catching built-ins at API boundaries requires string inspection (`"not found" in str(exc)`), which is fragile. A custom hierarchy lets the API layer write a single `except UploadError as exc` handler that produces a correct JSON response regardless of which upload stage failed.

The base `UploadError` also makes it trivial for callers to distinguish upload failures from other exceptions:

```python
except UploadError:
    # Expected failure -- return 4xx
except Exception:
    # Unexpected -- return 500 and log
```

## Known Gaps

`TooLarge` and `UnsupportedMime` do not provide default messages in their `__init__`, so callers must supply a message string when raising them. This inconsistency across the hierarchy could lead to exception raises with empty messages that provide no diagnostic context. A future improvement would add default messages to all leaf classes.