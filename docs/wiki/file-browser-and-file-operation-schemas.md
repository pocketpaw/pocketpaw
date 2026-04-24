---
{
  "title": "File Browser and File Operation Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's file-browser API, covering directory listings, path navigation, recent-file tracking from agent tool use, and file-write requests. These schemas form the contract between the FastAPI backend and the dashboard's file explorer UI.",
  "concepts": [
    "FileEntry",
    "BrowseResponse",
    "OpenPathRequest",
    "RecentFileEntry",
    "WriteFileRequest",
    "file browser",
    "Pydantic",
    "directory listing",
    "agent tool usage",
    "filesystem API"
  ],
  "categories": [
    "api-schemas",
    "file-management",
    "dashboard"
  ],
  "source_docs": [
    "6277ac7b28dcb7a6"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The file-browser subsystem exposes the agent's local filesystem to the dashboard, letting users browse directories, open files in their native explorer, inspect which files the agent recently touched, and overwrite file contents. Seven Pydantic models define the complete request/response surface for these operations.

## Models

### `FileEntry`

Represents a single filesystem node — file or directory — in a listing.

```python
class FileEntry(BaseModel):
    name: str
    isDir: bool = False
    size: str = ""
```

`size` is a pre-formatted string (e.g. `"12 KB"`) rather than raw bytes. This sidesteps the integer-overflow and locale-formatting concerns that arise when the UI tries to humanise byte counts itself, at the cost of making client-side sorting by size inaccurate.

### `BrowseResponse`

The directory-listing envelope.

```python
class BrowseResponse(BaseModel):
    path: str
    files: list[FileEntry] = []
    error: str | None = None
```

Returning `error` inline rather than relying solely on HTTP status codes means the front-end can display a user-friendly message without inspecting response status — important when the error is a permission-denied or path-not-found that should still return HTTP 200 from the router's perspective.

### `OpenPathRequest` / `OpenPathResponse`

Triggers the OS-level file explorer on the agent's machine.

```python
class OpenPathRequest(BaseModel):
    path: str
    action: str = "navigate"  # "navigate" or "view"
```

The `action` field distinguishes opening a folder (navigate) from revealing a specific file (view). Without this distinction the backend would need separate endpoints for two very similar operations.

### `RecentFileEntry` and `RecentFilesResponse`

Tracks files the agent accessed via its tools during a session.

```python
class RecentFileEntry(BaseModel):
    path: str
    name: str
    is_dir: bool = False
    extension: str = ""
    timestamp: float = 0
    tool: str = ""
```

`tool` records which agent tool (e.g. `read_file`, `write_file`, `bash`) produced the access. This is useful for debugging agent behaviour — the user can see exactly which action touched each file. `timestamp` as a Unix float enables chronological sorting without timezone parsing.

### `WriteFileRequest`

A destructive operation — complete file overwrite.

```python
class WriteFileRequest(BaseModel):
    path: str
    content: str
```

No diff or patch mechanism is offered; the client sends the full new content. This is intentionally simple: partial-update semantics would require the server to hold file state across requests, introducing concurrency risks if two sessions write the same file simultaneously.

## Defensive Patterns

- Optional `error` fields on response models prevent the API from throwing 500s for filesystem errors, keeping the dashboard responsive even when paths are inaccessible.
- All list fields default to `[]` so the front-end never receives `null` and can iterate unconditionally.

## Known Gaps

- `WriteFileRequest` has no content-length limit. Large payloads could exhaust memory or hit FastAPI's body-size limit silently.
- `isDir` uses camelCase while `is_dir` (on `RecentFileEntry`) uses snake_case — an inconsistency that could confuse consumers dealing with both models.
- No MIME-type or encoding field on `FileEntry` means the UI cannot decide how to preview a file before fetching its content.