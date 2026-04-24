---
{
  "title": "MediaDownloader: Filename Sanitization, Unique Naming, HTTP Download, and Size Limiting",
  "summary": "The `MediaDownloader` test suite validates PocketPaw's utility for safely downloading and storing media files from URLs, covering filename sanitization (path traversal prevention), collision-free unique filename generation, configurable size limits, HTTP error handling, and authenticated downloads. The singleton pattern ensures a single downloader instance manages all media across the application.",
  "concepts": [
    "MediaDownloader",
    "filename sanitization",
    "unique filename",
    "path traversal prevention",
    "HTTP download",
    "httpx",
    "size limit",
    "media directory",
    "build_media_hint",
    "singleton",
    "MIME type",
    "auth header"
  ],
  "categories": [
    "media handling",
    "file system",
    "test"
  ],
  "source_docs": [
    "67a35a1b1f3e2b42"
  ],
  "backlinks": null,
  "word_count": 491,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When an LLM tool or channel adapter needs to download a user-sent media file (e.g., an image URL in a Telegram message), `MediaDownloader` handles the HTTP fetch, filename sanitization, and disk persistence. The tests were added alongside the media attachment pipeline to ensure that downloaded files land in safe, predictable locations.

## Filename Sanitization

`_sanitize_filename(name)` strips or replaces characters that could cause filesystem or security issues:

- **Basic filename**: preserved as-is (`"photo.jpg"` → `"photo.jpg"`).
- **Special characters**: parentheses, spaces, and other shell-special chars are removed or replaced.
- **Empty string**: returns `"file"` as a safe fallback — an empty filename would cause `open()` to fail.
- **Path separators**: forward slashes are removed, preventing `"../../etc/passwd"` from writing to an arbitrary location. This is the primary path traversal defense at the filename level.

## Unique Filename Generation

`_unique_filename(name, mime_type)` produces a collision-resistant filename by combining a timestamp, a random hex component, and the sanitized original name:

```
{timestamp}_{hex8}_{sanitized_name}.ext
```

- **Extension from MIME**: if the original name has no extension, one is appended based on the MIME type (`image/png` → `.png`).
- **No collision**: two consecutive calls with the same input produce different filenames, verified by `test_unique_filename_no_collision`.

Without unique filenames, concurrent downloads of files with the same name would overwrite each other.

## Media Directory Resolution

`get_media_dir()` resolves the storage directory from settings:

- **Default**: a `generated/` subdirectory under the PocketPaw config directory.
- **Custom**: `settings.media_dir` overrides the default.

Both cases are tested with `tmp_path` fixtures to avoid touching the real config directory.

## Save from Bytes

`test_save_from_bytes` verifies that raw bytes can be written to a file in the media directory. `test_save_from_bytes_size_limit` confirms that a size limit is enforced — bytes exceeding the configured limit are rejected. `test_save_from_bytes_unlimited` verifies that `size_limit=0` disables the limit.

The size limit prevents a malicious sender from filling the server's disk by sending a very large file.

## HTTP Download

`test_download_url` uses `httpx.AsyncMock` to simulate a successful fetch and verifies the file is written with the correct content. `test_download_url_http_error` simulates an HTTP 404 and confirms the downloader raises or returns `None` rather than writing an empty file. `test_download_url_with_auth` verifies that an auth header is passed when credentials are provided.

`test_download_url_infers_name` covers the case where no explicit filename is given — the downloader should extract one from the URL path (`/uploads/photo.jpg` → `photo.jpg`).

## Media Hint Builder

`build_media_hint(paths)` generates the attachment hint string appended to LLM context:

- Empty list: empty string.
- Single file: `"\n[Attached: photo.jpg]"`.
- Multiple files: comma-separated.

## Singleton

`test_get_media_downloader_singleton` confirms that `get_media_downloader()` returns the same instance on repeated calls, preventing multiple downloaders from competing for the same file paths.

## Known Gaps

- There are no tests for download retry logic on transient network errors (connection reset, timeout). A single failure returns immediately.
- The size limit is enforced after the download completes (full response read into memory), not via streaming — very large files could exhaust memory before being rejected.