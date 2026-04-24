---
{
  "title": "Storage Key Generation: Namespaced, Date-Bucketed, UUID-Based Keys for Upload Blobs",
  "summary": "The `keys.py` module generates opaque storage keys with a predictable three-part structure -- kind, year-month bucket, and a UUID4 hex suffix -- that supports human-readable organization, time-based lifecycle policies, and guaranteed uniqueness. The companion `sanitize_ext()` function normalizes file extensions to prevent path injection through malformed extension strings.",
  "concepts": [
    "new_storage_key",
    "sanitize_ext",
    "storage key",
    "UUID4",
    "date bucketing",
    "kind namespace",
    "path injection",
    "extension sanitization",
    "S3 lifecycle",
    "yyyymm"
  ],
  "categories": [
    "uploads",
    "storage",
    "security",
    "key management"
  ],
  "source_docs": [
    "581cc83e938d441b"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a file is uploaded, PocketPaw must assign it a stable, unique key for storage. This key is the blob's address in whatever storage backend is active (local disk or S3). `keys.py` defines the key format and the utilities for generating and sanitizing them consistently across the entire upload pipeline.

## Key Structure

```
{kind}/{yyyymm}/{uuid32}{ext}
```

For example: `chat/202604/3f7a1b2c4d5e6f7a8b9c0d1e2f3a4b5c.png`

**`kind`** -- a logical namespace for the upload context (e.g., `"chat"` for chat-attached files, `"avatar"` for profile images). This groups related files in the same directory, making it easy to apply different S3 lifecycle policies per kind.

**`yyyymm`** -- the UTC year and month at upload time. Date bucketing prevents any single prefix from accumulating millions of objects, which can degrade S3 list-objects performance. It also enables time-based analysis and makes it straightforward to identify and expire old uploads at the storage level.

**`uuid32`** -- a UUID4 hex string (32 lowercase hex characters, no hyphens). UUID4 is randomly generated, providing statistical uniqueness without coordination between upload workers. No sequential IDs are used, which prevents enumeration attacks -- an attacker cannot guess adjacent file keys by incrementing a counter.

**`ext`** -- the sanitized file extension, appended for human readability and to help operating systems identify the file type when downloaded locally.

## Extension Sanitization

User-supplied filenames can contain malicious extensions: path traversal characters (`../`), null bytes, or excessively long strings. `sanitize_ext()` defends against all of these:

```python
_EXT_RE = re.compile(r"[^a-z0-9]")
_MAX_EXT_LEN = 8

def sanitize_ext(ext: str) -> str:
    if not ext:
        return ""
    tail = ext.lstrip(".").lower()
    tail = _EXT_RE.sub("", tail)[:_MAX_EXT_LEN]
    return f".{tail}" if tail else ""
```

1. `lstrip(".")` strips leading dots (e.g., `"..png"` -> `"png"`), preventing double-extension tricks.
2. `_EXT_RE.sub("", tail)` removes every character that is not ASCII alphanumeric -- eliminates path separators, null bytes, Unicode tricks.
3. `[:_MAX_EXT_LEN]` truncates to 8 characters -- a legitimate extension like `.jpeg` is 4 characters; anything longer is suspicious.
4. Returns empty string if nothing survives the sanitization, so keys for files with malicious extensions still get generated rather than failing.

## Separation of Concerns

`new_storage_key()` is intentionally stateless and has no side effects -- it reads the current UTC time and generates a UUID. This makes it trivially testable: you can call it thousands of times in a test suite without mocking. The storage adapter uses the returned key to write the blob; the file store uses it as the metadata record's `storage_key`.

## Known Gaps

The `kind` parameter defaults to `"chat"` but there is no validation that the provided kind string is safe to use as a path component. A kind value of `"../secrets"` would produce a traversal key -- the storage adapter's `LocalStorageAdapter` is responsible for preventing path traversal, but the key generator itself does not sanitize the kind. Applying the same alphanumeric sanitization to `kind` as is applied to `ext` would add defense in depth.