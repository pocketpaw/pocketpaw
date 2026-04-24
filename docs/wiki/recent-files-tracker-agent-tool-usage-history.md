---
{
  "title": "Recent Files Tracker: Agent Tool Usage History",
  "summary": "RecentFilesTracker intercepts agent tool calls to extract file paths and maintains a capped LRU list of the 50 most recently accessed files with metadata. It uses lazy loading, a heuristic Bash path extractor via regex, and JSON persistence to power the dashboard's recent files widget.",
  "concepts": [
    "RecentFilesTracker",
    "tool interception",
    "Bash heuristic",
    "lazy loading",
    "LRU",
    "file path extraction",
    "dashboard",
    "JSON persistence",
    "deduplication"
  ],
  "categories": [
    "file-tracking",
    "dashboard",
    "tooling"
  ],
  "source_docs": [
    "a7a0a3c98affc750"
  ],
  "backlinks": null,
  "word_count": 431,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`RecentFilesTracker` provides a passive observer for file access patterns across all agent tool invocations. The dashboard displays recently accessed files so users can quickly navigate back to files their agents have been working on. Without this tracker, there would be no way to surface this information — agent tool calls are fire-and-forget with no built-in history.

## Tool Path Extraction

The tracker knows which tools access files and which parameter key holds the path:

```python
_TOOL_PATH_KEYS: dict[str, list[str]] = {
    "Read": ["file_path"],
    "Write": ["file_path"],
    "Edit": ["file_path"],
    "read_file": ["path", "file_path"],
    "write_file": ["path", "file_path"],
    "edit_file": ["path", "file_path"],
    "str_replace_editor": ["path"],
    "Bash": [],  # handled separately via _extract_path_from_bash
}
```

The `Bash` tool is special because it takes a `command` string rather than a structured file path parameter. A dedicated heuristic extractor handles it.

## Bash Path Heuristic

The regex matches absolute paths (`/home/user/...`, `~/...`) and relative paths (`./...`, `../...`) in Bash command strings. Negative lookahead/lookbehind prevent matching words that happen to contain slashes. Trailing punctuation is stripped from matches to handle shell one-liners.

The heuristic accepts a candidate path if it exists on disk OR if the last path component has an extension (contains a dot). This means it accepts both existing files and files about to be created.

## Lazy Loading

`_ensure_loaded()` defers disk reads until the first `record_tool_use()` or `get_recent()` call. This avoids loading the file at import time, which would slow down every PocketPaw process startup even when recent files are never queried.

## Deduplication and Capping

On each `record_tool_use()`:
1. The existing entry for the same path is removed.
2. A new entry is inserted at position 0 (most recent).
3. The list is trimmed to `_MAX_ENTRIES = 50`.

This produces a true LRU-style list where the same file accessed multiple times only appears once, at the top.

## Stored Metadata

Each entry captures path, basename, is_dir flag, file extension (lowercase, dot stripped), Unix timestamp of access, and which tool accessed it. The extension is captured at write time so the dashboard can render language-appropriate icons without a filesystem call.

## Known Gaps

- **No file content**: The tracker stores paths, not contents. Diffing or showing file previews requires a separate read.
- **Bash heuristic can misfire**: Complex Bash pipelines can produce false positives (e.g., matching a URL path as a file path). The `p.exists() or "." in p.name` guard catches most cases but edge cases remain.
- **Single-process only**: The JSON file is not locked. Concurrent PocketPaw processes writing to `recent_files.json` simultaneously can corrupt it. The tracker catches JSON parse errors and resets to an empty list.