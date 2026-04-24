---
{
  "title": "Media Download Utility for Channel Adapters",
  "summary": "media.py provides a shared `MediaDownloader` singleton and related utilities that all channel adapters use to save incoming media attachments to local disk. It handles collision-free filename generation, file size enforcement, authenticated downloads, and MIME type guessing.",
  "concepts": [
    "MediaDownloader",
    "get_media_downloader",
    "collision-free filenames",
    "path traversal prevention",
    "file size limit",
    "MIME type guessing",
    "httpx.AsyncClient",
    "save_from_bytes",
    "download_url_with_auth",
    "build_media_hint",
    "singleton pattern"
  ],
  "categories": [
    "bus",
    "media",
    "file-handling",
    "utilities"
  ],
  "source_docs": [
    "3c2bad305a0e5029"
  ],
  "backlinks": null,
  "word_count": 556,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every channel adapter that receives media (images, documents, audio, video) delegates file storage to media.py rather than implementing its own download logic. This centralization ensures consistent behavior: all adapters write to the same directory, apply the same size limits, and generate filenames in the same format.

## Storage Location

`get_media_dir()` returns the media storage directory. The default is `~/.pocketpaw/media/` (via `get_config_dir()`), but this is overridable via `settings.media_download_dir`. The directory is created with `mkdir(parents=True, exist_ok=True)` on every call, so it is always guaranteed to exist when a download happens.

## Collision-Free Filenames

`_unique_filename()` generates filenames in the format `{timestamp_hex}_{hash8}_{sanitized_name}`. The timestamp component (milliseconds since epoch in hex) provides rough chronological ordering. The hash8 component is derived from `time.time_ns()`, `os.urandom(8)`, and the original filename — making collisions statistically impossible even for simultaneous downloads of files with identical names. Without this, concurrent downloads of `photo.jpg` from two different users would overwrite each other.

`_sanitize_filename()` removes all characters except alphanumeric, dots, hyphens, and underscores, then collapses repeated underscores. This prevents path traversal (e.g., `../../etc/passwd`) and filesystem-illegal filenames.

## MIME Type and Extension Guessing

If the original filename has no extension, `_unique_filename()` calls `mimetypes.guess_extension(mime)` to derive one. This matters for files named generically (e.g., WhatsApp sends audio messages as `"audio"` with MIME `"audio/ogg"`), ensuring the saved file has a usable `.ogg` extension for downstream processing.

## File Size Enforcement

`_check_size()` raises `ValueError` if the downloaded data exceeds `settings.media_max_file_size_mb`. This guard is applied after the bytes are in memory but before they are written to disk. The check applies to both `save_from_bytes()` and `download_url()`. Setting `media_max_file_size_mb = 0` disables the check entirely.

## Download Methods

Three download paths cover different adapter needs:

- **`save_from_bytes()`** — for adapters that provide raw bytes directly (Telegram, neonize/whatsmeow)
- **`download_url()`** — for public or unprotected URLs (Discord, Signal attachments)
- **`download_url_with_auth()`** — thin wrapper that injects an `Authorization` header; used by Slack (Bearer bot token) and WhatsApp Business Cloud API

All three converge on the same filename generation and disk-write path.

## HTTP Client Lifecycle

The `MediaDownloader` lazily creates an `httpx.AsyncClient` on first use and reuses it for all subsequent downloads. The client is configured with a 60-second timeout and `follow_redirects=True` (necessary for some CDN URLs). If the client is closed externally, `_get_client()` detects `is_closed` and recreates it.

`close()` explicitly closes the client. It is called during adapter shutdown to release file descriptors and connection pools cleanly.

## Singleton Pattern

`get_media_downloader()` returns a module-level `_downloader` singleton. All adapters share the same `httpx.AsyncClient` connection pool, reducing per-download TCP handshake overhead. The singleton is reset by setting `_downloader = None`, which causes the next `get_media_downloader()` call to create a fresh instance.

## build_media_hint

`build_media_hint(filenames)` produces a text string like `"\n[Attached: photo.jpg, doc.pdf]"` that is appended to the message content before publishing to the bus. This ensures the LLM receives a textual reference to any attached files alongside their content, making it possible to answer questions about the attachment even if the file is not passed directly to the model.

## Known Gaps

- Media files accumulate on disk indefinitely — there is no built-in eviction or cleanup policy. Long-running deployments will eventually exhaust disk space without an external cleanup cron job.
- `_check_size()` loads the full file into memory before checking size; a streaming download with an early abort would be more memory-efficient for large files.