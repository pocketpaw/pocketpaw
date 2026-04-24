---
{
  "title": "Upload Configuration: Size Limits, MIME Allowlist, and Inline Rendering Rules",
  "summary": "The `uploads/config.py` module centralizes all static configuration for the upload pipeline -- maximum file size, per-batch file count, allowed MIME types, and local storage root -- while also defining which MIME types are safe for inline browser rendering versus forcing an attachment download. The `extension_for()` helper maps canonical MIME types to file extensions for consistent key generation.",
  "concepts": [
    "UploadSettings",
    "MIME allowlist",
    "INLINE_MIMES",
    "Content-Disposition",
    "max_file_bytes",
    "extension_for",
    "frozenset",
    "security allowlist",
    "XSS prevention",
    "file extensions"
  ],
  "categories": [
    "uploads",
    "security",
    "configuration"
  ],
  "source_docs": [
    "3e0a1afb316322a4"
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

Every layer of the upload pipeline needs to agree on what files are acceptable and how to handle them. Rather than scattering size limits and MIME type lists throughout route handlers, validators, and storage code, `config.py` defines a single authoritative source. This makes security-relevant limits easy to audit.

## UploadSettings

`UploadSettings` is a dataclass with four fields:

```python
@dataclass
class UploadSettings:
    max_file_bytes: int = 25 * 1024 * 1024      # 25 MiB
    max_files_per_batch: int = 50
    allowed_mimes: frozenset[str] = DEFAULT_ALLOWED_MIMES
    local_root: Path = Path.home() / ".pocketpaw" / "uploads"
```

All fields have defaults so instantiating `UploadSettings()` gives a sensible production configuration. Individual fields can be overridden in tests or specialized deployments.

## Two MIME Lists: Allowed vs. Inline

The module maintains two separate frozensets with distinct purposes:

**`DEFAULT_ALLOWED_MIMES`** -- the security allowlist of MIME types the upload pipeline will accept at all. Anything not in this set is rejected before the bytes are written to storage. It covers common image formats, PDF, Word/Excel documents, plain text, Markdown, CSV, and JSON.

**`INLINE_MIMES`** -- a narrower subset of types safe to serve with `Content-Disposition: inline` (rendered in the browser). HTML and SVG are notably absent from this list even though they are text-based, because they can execute JavaScript and perform origin-based attacks when served inline from the same origin as the PocketPaw dashboard. SVG in particular is a common XSS vector via embedded `<script>` tags. Files not in `INLINE_MIMES` get `Content-Disposition: attachment`, forcing a download dialog.

## The extension_for Helper

```python
def extension_for(mime: str) -> str:
    return _MIME_TO_EXT.get(mime, "")
```

Storage keys are generated from UUID4 hex values. Without appending an extension, all stored files look the same at the filesystem level. `extension_for` provides the canonical extension for each allowed MIME type, enabling `new_storage_key("chat", ".png")` to produce readable keys like `chat/202604/abc123.png`. The function returns an empty string for unknown MIME types rather than raising, because key generation must not fail.

## Why frozenset

Both MIME collections are `frozenset` rather than `set` or `list`. Frozenset membership checks are O(1), which matters when every upload request calls this check. Being immutable also prevents accidental mutation during request handling.

## Known Gaps

The 25 MiB limit is hardcoded and not configurable via environment variable, requiring a code change to adjust for deployments that need larger uploads. `DEFAULT_ALLOWED_MIMES` does not include audio or video MIME types, so the upload system cannot handle voice message attachments without a config override.