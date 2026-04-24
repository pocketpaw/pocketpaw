---
{
  "title": "Upload Error Hierarchy Tests: Typed Exception Codes and Inheritance",
  "summary": "This module validates the upload error hierarchy in `pocketpaw.uploads.errors`, ensuring all domain-specific exceptions inherit from `UploadError`, carry typed `code` attributes, and preserve error messages. These contracts allow API routes and clients to handle upload failures programmatically without string matching.",
  "concepts": [
    "UploadError",
    "TooLarge",
    "UnsupportedMime",
    "EmptyFile",
    "NotFound",
    "AccessDenied",
    "StorageFailure",
    "typed exceptions",
    "error code",
    "exception hierarchy",
    "uploads"
  ],
  "categories": [
    "testing",
    "uploads",
    "error handling",
    "exceptions",
    "test"
  ],
  "source_docs": [
    "229277cee9553fac"
  ],
  "backlinks": null,
  "word_count": 405,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_errors.py` tests the typed exception hierarchy used throughout PocketPaw's upload subsystem. The tests are deliberately simple but load-bearing: they lock down the contracts that API routes, storage adapters, and clients rely on when handling upload failures.

## Why a Typed Error Hierarchy Matters

Upload operations can fail in many ways: the file is too large, the MIME type is not allowed, the file is empty, the record is not found, the caller lacks permission, or the storage backend fails. Without a typed hierarchy, code would need to catch broad exceptions (`Exception`, `IOError`) and inspect message strings to distinguish failure modes—fragile, locale-sensitive, and hard to test.

With a hierarchy rooted at `UploadError`, callers can:

```python
try:
    await adapter.put(key, stream, mime)
except TooLarge:
    return 413
except UnsupportedMime:
    return 415
except UploadError:
    return 500
```

## Test: Inheritance (`test_all_errors_inherit_upload_error`)

Asserts that every concrete error class is a subclass of `UploadError`:

- `TooLarge`
- `UnsupportedMime`
- `EmptyFile`
- `NotFound`
- `AccessDenied`
- `StorageFailure`

This allows `except UploadError` to serve as a safe catch-all in routes that want to convert any upload failure into a structured JSON error without exposing implementation details.

## Test: Typed Code Attributes (`test_errors_carry_code_attribute`)

Each exception carries a `code` string attribute:

| Class | code |
|-------|------|
| `TooLarge` | `"too_large"` |
| `UnsupportedMime` | `"unsupported_mime"` |
| `EmptyFile` | `"empty"` |
| `NotFound` | `"not_found"` |
| `AccessDenied` | `"access_denied"` |
| `StorageFailure` | `"storage_error"` |

The `code` attribute is what the API router serializes into the JSON error response (`{"code": "too_large", "detail": "..."}`). Clients and frontend code key off this string to display localized messages. If the `code` values drifted from these expected strings, the frontend would silently show the wrong error message or miss a case entirely.

## Test: Message Preservation (`test_upload_error_preserves_message`)

`TooLarge("file is 40MB")` must produce `str(err) == "file is 40MB"`. This confirms that the exception constructors do not swallow or transform the detail message, which would make server logs uninformative.

## Known Gaps

- No test verifies that `code` is a class-level attribute (not instance-level), meaning a subclass that sets `self.code` in `__init__` rather than as a class variable would pass these tests but could behave unexpectedly if `code` is accessed on the class itself.
- `StorageFailure` is the only class whose `code` value (`"storage_error"`) does not directly mirror the class name (`storage_failure`). This inconsistency is untested as a design choice—future code searching for `"storage_failure"` would not find it.
