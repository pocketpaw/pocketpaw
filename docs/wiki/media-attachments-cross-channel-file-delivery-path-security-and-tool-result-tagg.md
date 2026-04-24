---
{
  "title": "Media Attachments: Cross-Channel File Delivery, Path Security, and Tool Result Tagging",
  "summary": "The media attachments test suite validates PocketPaw's end-to-end media pipeline — from tool result tagging via HTML-comment markers, through agent loop extraction, to per-channel delivery (Telegram, Discord, Slack, WhatsApp, WebSocket) and the secure `/api/media` serving endpoint. Path traversal protection for the media endpoint receives dedicated security coverage.",
  "concepts": [
    "media attachments",
    "BaseTool._media_result",
    "HTML comment tagging",
    "_extract_media_paths",
    "guess_media_type",
    "Telegram",
    "Discord",
    "Slack",
    "WhatsApp",
    "WebSocket",
    "path traversal",
    "media serving endpoint",
    "channel adapters"
  ],
  "categories": [
    "media handling",
    "security",
    "test"
  ],
  "source_docs": [
    "412e776c81f4dfe4"
  ],
  "backlinks": null,
  "word_count": 523,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a PocketPaw tool generates a file (audio, image, document), it needs to deliver that file to the user through whatever channel they are on. The pipeline has three stages: the tool tags its result with a media path marker, the agent loop extracts those markers after receiving the tool output, and the channel adapter sends the file using the channel's native upload API. The test suite covers each stage and the security boundary around file serving.

## Stage 1: Tool Result Tagging

`TestMediaResult` tests `BaseTool._media_result(path, text)`, which wraps a file path in an HTML comment tag:

```python
"<!-- media:/tmp/audio.wav -->Audio generated (1234 bytes)"
```

The HTML comment format was chosen because it is invisible in rendered markdown but parseable by regex. Tests cover:

- **With text**: comment + text.
- **No text**: comment only (no trailing space).
- **Empty text string**: same as no text — whitespace is stripped to prevent `"<!-- media:... --> "` with a trailing space that could confuse parsers.

## Stage 2: Agent Loop Extraction

`TestExtractMediaPaths` tests `_extract_media_paths(text)`, which finds all `<!-- media:... -->` tags in a string and returns the paths as a list. Tests cover single, multiple, no tags, empty string, and paths with spaces (which require the regex to not stop at the first space).

## Media Type Guessing

`TestGuessMediaType` tests `guess_media_type(filename)`, which maps file extensions to `audio`, `image`, or `document` (fallback). Tests cover known audio extensions, image extensions, document fallback, and case insensitivity (`.JPG` == `.jpg`).

## Stage 3: Per-Channel Delivery

Each channel adapter has its own `_send_media_file()` implementation that uses the channel's native upload API:

- **Telegram** (`TestTelegramMediaSend`): uses `send_audio`, `send_photo`, or `send_document` based on media type. Tests verify correct method selection, topic-aware sending (forum threads), and skipping of missing files.
- **Discord** (`TestDiscordMediaSend`): sends via the `_send_command` internal path; `test_sends_media_on_stream_end` verifies that media is sent when the stream ends, not mid-stream.
- **Slack** (`TestSlackMediaSend`): uses the files upload API; missing files are skipped.
- **WhatsApp** (`TestWhatsAppMediaSend`): uploads then sends a media message reference.

The "skip missing file" pattern appears across all adapters. If the tool failed to create the file (e.g., TTS service error), the adapter must not crash — it silently omits the attachment and the text-only response is delivered.

## WebSocket Media Payload

`TestWebSocketMedia` verifies that `stream_end` events include a `media` array in the WebSocket payload when media paths are present, and an empty/absent array when there are none. The dashboard UI uses this to render media inline.

## Security: Path Traversal Protection

`TestServeMediaSecurity` tests the `/api/media` endpoint:

- **Outside generated dir**: a path that resolves outside the `generated/` directory is rejected with 403.
- **Traversal sequence**: a path containing `../` sequences is rejected.
- **Missing file**: a valid-looking path that does not exist on disk returns 404.

The traversal protection prevents a malicious client from reading arbitrary files from the server's filesystem by crafting a path like `../../../etc/passwd`.

## Known Gaps

- Large file upload behavior (size limits, chunked upload) is not tested for any channel adapter.
- The WebSocket media payload format is tested but the client-side rendering of the `media` array is not validated here.