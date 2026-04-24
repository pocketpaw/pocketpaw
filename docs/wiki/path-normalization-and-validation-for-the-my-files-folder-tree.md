---
{
  "title": "Path Normalization and Validation for the My Files Folder Tree",
  "summary": "This module provides a small set of path utilities that enforce a strict, well-defined path format for the uploads folder tree. All paths must be absolute, forward-slash separated, with no trailing slash, no traversal sequences, and no control characters — preventing path injection and ensuring consistent storage key formatting across the uploads subsystem.",
  "concepts": [
    "path normalization",
    "path traversal prevention",
    "normalize_path",
    "is_subpath",
    "join_path",
    "parent_of",
    "control character validation",
    "virtual filesystem",
    "segment validation",
    "folder tree"
  ],
  "categories": [
    "uploads",
    "security",
    "utilities",
    "cloud EE"
  ],
  "source_docs": [
    "e534aa26cc068a38"
  ],
  "backlinks": null,
  "word_count": 435,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/paths.py` defines the canonical path contract for the My Files virtual filesystem. Every path stored in MongoDB or compared in queries must pass through these utilities to guarantee a single normalized form.

## Why Strict Path Normalization

Without normalization, paths like `/reports//2026/`, `/reports/./2026`, and `/reports/2026` would all be treated as distinct database entries pointing to the same logical folder. Users could create duplicate folders, and operations like `is_subpath` would give false negatives due to trailing slashes or repeated separators.

More critically, accepting `..` segments or control characters in path inputs opens the door to path traversal attacks — a user who can store `/../admin` as a folder path might be able to manipulate queries that later use the path string as a filesystem prefix.

## normalize_path

`normalize_path` is the main entry point for any externally-supplied path string. It handles `None`, empty strings, and `"/"` as root; enforces `len(p) <= 1024`; rejects null bytes; requires the path to start with `"/"`; collapses `"."` segments; rejects `".."` outright; and calls `_validate_name` on each segment. The result always starts with `"/"` and has no trailing slash.

## _validate_name: Segment-Level Guards

Each path segment passes through `_validate_name` which checks:

1. Non-empty (no `//`)
2. Not `"."` or `".."`
3. No forward or backward slashes within the segment
4. Maximum length of 255 characters
5. No control characters (code points 0x00 through 0x1F and 0x7F)

The control character check is particularly important for filesystem-backed adapters: a segment containing a null byte or a newline could corrupt filenames on ext4 or cause unexpected behavior on NTFS.

## is_subpath: Subtree Membership

```python
def is_subpath(ancestor: str, p: str) -> bool:
    return p == ancestor or p.startswith(ancestor.rstrip("/") + "/")
```

This is used by `soft_delete_under_prefix` and `rewrite_path_prefix` to determine which folders or files fall under a given parent. The `rstrip("/")` guard handles the root `"/"` edge case correctly — `is_subpath("/", "/reports")` returns `True`.

## join_path and parent_of

`join_path(parent, name)` validates `name` through `_validate_name` before concatenating, preventing unvalidated user input from bypassing normalization via the join path.

`parent_of` returns the parent of a given path. For `"/reports/2026"` it returns `"/reports"`. For `"/"` it returns `"/"` — the root is its own parent, which simplifies recursive callers that walk up the tree.

## Known Gaps

- Paths are case-sensitive. `normalize_path("/Reports")` and `normalize_path("/reports")` are treated as different folders. On case-insensitive filesystems (macOS default HFS+), this could create confusion if the storage adapter maps paths to real directories.
- There is no maximum depth limit on paths. A path with hundreds of segments would pass validation as long as the total length stays under 1024 characters.