---
{
  "title": "Files API Tests: Browse, Download, Zip, Write, and Path Traversal Defence",
  "summary": "This test file covers PocketPaw's `/api/v1/files` router, which exposes local filesystem operations (browse, single-file download, directory zip download, and file write) to the dashboard. It places particular emphasis on path traversal prevention, file jail enforcement, and zip bomb mitigations.",
  "concepts": [
    "file jail",
    "is_safe_path",
    "path traversal",
    "browse files",
    "download",
    "zip download",
    "zip bomb mitigation",
    "RFC 5987 filename encoding",
    "hidden file filtering",
    "write file",
    "_ZIP_MAX_FILES",
    "_ZIP_MAX_BYTES",
    "Content-Disposition"
  ],
  "categories": [
    "file system",
    "security",
    "API",
    "testing",
    "test"
  ],
  "source_docs": [
    "f5f0faf3b8c0d6a2"
  ],
  "backlinks": null,
  "word_count": 560,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's files API lets the dashboard browse and transfer files on the host machine — a powerful capability that requires strict access control. All routes use `is_safe_path` from `pocketpaw.tools.fetch` to verify that a requested path falls within the configured `file_jail_path`. This test file exercises both the happy paths and the full range of rejection scenarios.

## Browse (`GET /files/browse`)

`TestBrowseFiles` covers six scenarios:

- **Home directory expansion**: A path of `"~"` expands to the home directory. The test patches `Path.home()` to point to a temp directory, keeping the test hermetic.
- **Access denied**: When `is_safe_path` returns `False` (simulating a path outside the jail like `/etc/shadow`), the response is still 200 but contains an `error` field with "access denied". Returning 200 with a structured error body — rather than 403 — allows the dashboard to display a user-friendly message without triggering browser-level error handling.
- **Non-existent path**: Returns 200 with an error mentioning "not exist".
- **Hidden file filtering**: Entries whose names start with `.` are excluded from the listing. This prevents the dashboard from accidentally exposing dotfiles like `.env` or `.ssh/id_rsa`.
- **File size in response**: Each entry includes a human-readable size string (e.g. "100 B"), not just a raw byte count.
- **Directories sorted first**: The listing puts directories before files regardless of alphabetical order, matching the convention of most file managers.

## Download (`GET /files/download`)

`TestDownloadFile` covers five scenarios:

- **Returns file content**: A successful download returns the file bytes with `Content-Disposition: attachment; filename="..."` so browsers prompt a save dialog rather than rendering the file inline (which could execute scripts in HTML or SVG files).
- **Non-existent file**: Returns 404.
- **Path traversal**: When `is_safe_path` returns `False`, the route returns 403 (not 200 with an error), because this is an active security violation rather than a user error.
- **Directory rejected**: Attempting to download a directory path returns 400.
- **RFC 5987 filename encoding**: A filename containing non-ASCII characters (e.g. `"café report.txt"`) must be encoded with `filename*=UTF-8''...` in the Content-Disposition header, per RFC 5987. Without this, browsers may corrupt the filename on download.

## Zip Download (`GET /files/download-zip`)

`TestDownloadZip` adds two safety limits:

```python
with patch("pocketpaw.api.v1.files._ZIP_MAX_FILES", 2):
    # 3 files in directory -> 413
```

- **File count cap** (`_ZIP_MAX_FILES`): Prevents a directory with thousands of files from generating a request that consumes all available memory during zip assembly. Returns 413 with "Too many files" detail.
- **Cumulative size cap** (`_ZIP_MAX_BYTES`): Even if the file count is within bounds, the total uncompressed size is checked. Returns 413 with "size exceeds" detail. This mitigates zip bomb scenarios where many small files compress to a manageable archive but decompress to gigabytes.
- Path traversal and non-directory inputs also return 403 and 400 respectively.

## Write (`POST /files/write`)

`TestWriteFile` enforces that the write endpoint is a strict edit-only operation:

- **Existing file updated**: A file that already exists can be overwritten. Returns `{"ok": true}`.
- **Non-existent file rejected**: Creating new files is not allowed — returns 404. This prevents an attacker from writing arbitrary new files to the jail.
- **Path traversal**: Returns 403.
- **Directory rejected**: Returns 400.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover concurrent write access to the same file, symlink handling within the jail, or what happens when the jail path itself does not exist at startup.