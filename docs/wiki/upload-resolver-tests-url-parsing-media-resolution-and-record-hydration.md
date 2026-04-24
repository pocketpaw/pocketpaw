---
{
  "title": "Upload Resolver Tests: URL Parsing, Media Resolution, and Record Hydration",
  "summary": "This module tests `pocketpaw.uploads.resolver`, which converts upload URLs (like `/api/v1/uploads/{id}`) into hydrated `ResolvedMedia` objects containing the file's storage path, MIME type, and metadata. It validates URL parsing precision, resolver lookup behavior, and the convenience helpers for bulk resolution.",
  "concepts": [
    "UploadResolver",
    "parse_upload_url",
    "ResolvedMedia",
    "FileRecord",
    "resolve_media_paths",
    "resolve_media_with_records",
    "upload URL",
    "metadata lookup",
    "LocalStorageAdapter",
    "JSONLFileStore",
    "soft delete"
  ],
  "categories": [
    "testing",
    "uploads",
    "resolver",
    "URL parsing",
    "media handling",
    "test"
  ],
  "source_docs": [
    "9e1ea45e229858d6"
  ],
  "backlinks": null,
  "word_count": 452,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_resolver.py` tests `pocketpaw.uploads.resolver`. The resolver bridges the gap between the string URL references that appear in agent messages (e.g., `/api/v1/uploads/abc123`) and the actual binary data and metadata stored on disk or in S3. Without the resolver, the agent runtime would need to parse upload URLs and access storage directly—coupling it to the storage layer.

## URL Parsing (`TestParseUploadUrl`)

`parse_upload_url(url)` extracts the file ID from a canonical upload URL or returns `None` for non-upload strings.

### Positive Cases

- `/api/v1/uploads/{hex_id}` extracts `hex_id`. The test uses a real `uuid4().hex` to match expected production IDs.
- `/api/v1/uploads/abc` — permissive: any non-empty segment is accepted. Validity is deferred to the metadata lookup, not the parser.
- `/api/v1/uploads/not-hex-chars` — also accepted; the parser is intentionally lenient to avoid false negatives on unusual IDs.

### Negative Cases

- `/api/v1/files/abc` — different path prefix → `None`.
- `https://example.com/x` — absolute URL → `None`.
- `/home/user/image.png` — disk path → `None`. This prevents the resolver from accidentally treating local paths as upload references, which could expose arbitrary files.
- `C:ooar.pdf` — Windows disk path → `None`.
- `/api/v1/uploads/abc/` — trailing slash → `None`. Prevents ambiguous matches.
- `/api/v1/uploads/abc/download` — extra segment → `None`.
- `""` — empty string → `None`.

The strictness on trailing slashes and extra segments prevents false positives that could cause the resolver to attempt a metadata lookup on a path that has no corresponding record.

## Resolver (`TestUploadResolver`)

### Fixture and `_stash` Helper

The `resolver` fixture constructs an `UploadResolver` backed by a `LocalStorageAdapter` and a `JSONLFileStore`, both pointing at `tmp_upload_root`. The `_stash` helper writes bytes to disk and registers metadata, simulating the result of a completed upload without going through the full upload API.

### Resolution Tests

- **Successful resolution**: A stashed file can be resolved by its upload URL. The `ResolvedMedia` object contains the file's storage key, MIME type, and the `FileRecord` with owner and size information.
- **Missing record**: A URL with an unknown ID returns `None` (not an exception), allowing callers to treat missing uploads gracefully.
- **Soft-deleted record**: A deleted file returns `None` even if the bytes still exist on disk.

### Bulk Helpers

- `resolve_media_paths`: Scans a list of strings, resolves upload URLs, and returns `ResolvedMedia` objects for matched items, passing non-URL strings through unchanged.
- `resolve_media_with_records`: Similar but returns `(ResolvedMedia, FileRecord)` pairs for callers that need both the storage handle and the metadata.

## Known Gaps

- No test covers the case where the metadata record exists but the backing file is missing from storage (record/blob desync), which could occur after manual storage cleanup.
- `resolve_media_paths` behavior when the same URL appears multiple times in the list is untested—it may resolve it twice, incurring redundant metadata lookups.
