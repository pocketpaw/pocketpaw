---
{
  "title": "Files Router — Scoped File Browser, Download, and Write with Path Traversal Guards",
  "summary": "The files router gives the PocketPaw dashboard a full file browser over the agent's accessible filesystem: directory listing, file content serving, single-file download, directory-as-zip download, and file writing. Security hardening in April 2026 added scope guards and symlink filtering to prevent path traversal attacks identified in issues #884 and #886.",
  "concepts": [
    "file browser",
    "path traversal",
    "symlink filter",
    "files:read scope",
    "files:write scope",
    "zip download",
    "RFC 5987",
    "Content-Disposition",
    "jail",
    "recent files",
    "agent file access"
  ],
  "categories": [
    "API",
    "Security",
    "Files"
  ],
  "source_docs": [
    "d7043a8bfcaf6f4a"
  ],
  "backlinks": null,
  "word_count": 423,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Agents frequently read and write files as part of their work. The files router exposes these file system operations to dashboard users and external API clients, providing a safe, scoped window into the agent's working directories. The design prioritizes preventing path traversal while keeping the interface ergonomic.

## Scope Guards at the Router Level

```python
router = APIRouter(tags=["Files"], dependencies=[Depends(require_scope("files:read"))])
```

The `files:read` scope is required for all endpoints. The `write_file` endpoint additionally requires `files:write`. This router-level guard (added 2026-04-16, closing issues #884 and #886) ensures that API keys without explicit file access cannot browse or exfiltrate arbitrary paths — a common attack vector when agents write secrets to working directories.

## Path Resolution and Jail

`_resolve_path(path)` converts any client-supplied path string to an absolute `Path`. The implementation resolves `~` to the home directory and calls `.resolve()` to collapse `../` sequences. The write endpoint enforces a "jail" — files can only be written within an explicitly permitted directory. Writes to paths outside this jail are rejected with HTTP 403.

## Symlink Filtering in ZIP Downloads

The `download_dir_as_zip` endpoint recursively packages a directory as a zip archive. Without symlink filtering, an attacker could create a symlink inside a permitted directory pointing to `/etc/passwd` or any sensitive path, then trigger a ZIP download to exfiltrate it. The 2026-04-16 update adds symlink exclusion during directory traversal:

```python
# Symlinks excluded from zip to prevent path traversal via symlink (closes #886)
```

## RFC 5987 Content-Disposition

`_content_disposition(filename)` builds a properly encoded `Content-Disposition: attachment` header using RFC 5987 percent-encoding for non-ASCII filenames. Without this, filenames containing spaces, Unicode characters, or special characters would be mangled or rejected by browser download handlers.

## Recent Files from Agent Tool Usage

`get_recent_files` returns a list of files recently accessed by agent tools, sourced from the agent's tool-use journal rather than the OS `atime`. This gives the dashboard a meaningful "recently used" list that reflects what the agent has been doing rather than arbitrary filesystem access times.

## Write Endpoint Constraints

`write_file` enforces two constraints beyond scope:
1. **Text-only**: Binary file writes are rejected. This prevents the endpoint from being used to overwrite executables or binary configs.
2. **Jail**: Only paths within the permitted working directory are writable.

## Known Gaps

The `open_path` endpoint pushes an `open_path` event to WebSocket clients, which triggers their OS to open the file in the default application. This is a purely local operation and doesn't validate that the path is safe before broadcasting — any `files:read`-scoped key can trigger a file-open event on the dashboard.