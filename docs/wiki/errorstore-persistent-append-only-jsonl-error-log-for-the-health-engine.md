---
{
  "title": "ErrorStore — Persistent Append-Only JSONL Error Log for the Health Engine",
  "summary": "`ErrorStore` is PocketPaw's persistent error logging backend, storing health engine errors as newline-delimited JSON in `~/.pocketpaw/health/errors.jsonl` with log rotation, tail-read retrieval, and optional search filtering. It survives server restarts and page refreshes, ensuring operators can inspect errors from previous sessions.",
  "concepts": [
    "ErrorStore",
    "JSONL",
    "append-only log",
    "log rotation",
    "error persistence",
    "tail-read",
    "search filter",
    "uuid error ID",
    "health engine",
    "disk storage"
  ],
  "categories": [
    "health monitoring",
    "storage"
  ],
  "source_docs": [
    "b213e68277d8d9da"
  ],
  "backlinks": null,
  "word_count": 566,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ErrorStore` solves a specific problem: the health engine runs checks at startup and during operation, but if the dashboard is refreshed or the server restarts, in-memory results are lost. `ErrorStore` persists errors to disk in a durable append-only JSONL format so they can be reviewed retroactively.

## Storage Format

Each error is written as one JSON object per line to `~/.pocketpaw/health/errors.jsonl`:

```json
{"id": "a3b4c5d6e7f8", "timestamp": "2026-04-23T10:15:30Z", "source": "connectivity", "severity": "error", "message": "Cannot reach Anthropic API: ConnectionError", "traceback": "...", "context": {}}
```

JSONL (newline-delimited JSON) is chosen over a structured database because:
- Append operations are atomic at the OS level for small writes
- The file can be read and parsed with standard tools (`jq`, `grep`)
- No schema migrations needed when the error structure changes
- Log rotation is file-level (rename/unlink), not record-level

## Error Recording

```python
def record(self, message, source="unknown", severity="error", traceback="", context=None) -> str:
    error_id = uuid.uuid4().hex[:12]
    entry = {...}
    with self._path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return error_id
```

The generated `error_id` (12-character hex UUID prefix) is returned to callers so they can reference specific errors in logs or UI. The `default=str` in `json.dumps` ensures that any non-serializable value in `context` (e.g., a datetime, an exception object) is safely converted to a string rather than raising a `TypeError`.

Write failures are caught and logged to Python's standard logger — an error store that crashes on write would be worse than one that silently drops entries. The caller still receives an empty string as the error ID, indicating the record was not persisted.

## Tail-Read Retrieval with Search

`get_recent()` reads the file and reverses the lines to return newest-first:

```python
lines = self._path.read_text(encoding="utf-8").strip().splitlines()
for line in reversed(lines):
    entry = json.loads(line)
    if search and search.lower() not in haystack:
        continue
    results.append(entry)
    if len(results) >= limit:
        break
```

Reading the entire file and reversing in memory is simple but not efficient for very large log files. The rotation mechanism (below) keeps the file bounded at 10 MB, so this trade-off is acceptable.

The `search` parameter filters against a concatenation of `message + source + traceback`, enabling quick incident lookups without an index.

## Log Rotation

`rotate_if_needed()` checks the file size and rotates when it exceeds `_DEFAULT_MAX_SIZE_MB` (10 MB):

```python
_MAX_ROTATION_FILES = 5
_DEFAULT_MAX_SIZE_MB = 10
```

Rotation uses a sliding rename strategy (`.1` → `.2` → `.3` ... → `.5`), dropping the oldest when the maximum rotation count is reached. This bounds total disk usage to approximately 50 MB across all rotation files.

The rotation is not automatic — callers must explicitly call `rotate_if_needed()`. The health engine is expected to call this periodically (e.g., on each startup check run).

## clear() for Testing

```python
def clear(self) -> None:
    if self._path.exists():
        self._path.unlink()
```

This method exists specifically for test isolation, allowing test suites to wipe the error log between test runs without leaving state leakage.

## Known Gaps

- `get_recent()` reads the entire file into memory before filtering. For files near the 10 MB limit, this could use significant memory. A true tail-read implementation would seek from the end of the file.
- Rotation is not called automatically — if the health engine does not call `rotate_if_needed()` on schedule, the log file can grow unbounded until the next manual call.
- There is no file locking on write, which means concurrent server processes writing to the same `errors.jsonl` could produce interleaved partial lines.
