---
{
  "title": "JSONL File Store: Append-Only Metadata Persistence for Uploaded Files",
  "summary": "The `JSONLFileStore` provides a lightweight, append-only metadata store for file upload records using newline-delimited JSON, with an in-memory cache for fast lookups and thread-safe writes via a `Lock`. Soft deletes are implemented by appending a delete operation record rather than removing lines, preserving the full audit history of every file lifecycle event.",
  "concepts": [
    "JSONLFileStore",
    "FileRecord",
    "append-only",
    "JSONL",
    "soft delete",
    "in-memory cache",
    "thread safety",
    "Lock",
    "_reload",
    "audit trail",
    "datetime serialization"
  ],
  "categories": [
    "uploads",
    "storage",
    "data persistence",
    "metadata"
  ],
  "source_docs": [
    "e5a0e5de0d75be7e"
  ],
  "backlinks": null,
  "word_count": 439,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Every uploaded file has associated metadata (original filename, MIME type, size, owner, chat context, upload timestamp) that must survive process restarts and be efficiently retrievable by file ID. `file_store.py` implements this persistence using a JSONL (newline-delimited JSON) file -- a format that is human-readable, easily parseable, and appendable without in-place modifications.

## Append-Only Design

JSONL is append-only by nature: new records are written by opening the file in append mode (`"a"`), writing a JSON line, and closing. This has two advantages:

1. **No corruption from partial writes** -- a crash mid-write leaves an incomplete last line, which `_reload()` skips with a `json.JSONDecodeError` warning. All previous records remain intact.
2. **Audit trail** -- delete operations are recorded as `{"op": "delete", "id": ..., "at": ...}` lines rather than physical removal. The complete history of saves and deletes is preserved in the file.

## In-Memory Cache

At startup, `_reload()` reads the entire JSONL file and builds two in-memory data structures: `self._records` (a dict of `file_id -> FileRecord`) and `self._deleted` (a set of deleted file IDs). Subsequent `get()` calls are pure in-memory lookups -- no disk I/O per request.

The reload handles malformed lines gracefully:

```python
try:
    row = json.loads(line)
except json.JSONDecodeError:
    logger.warning("skipping corrupt upload-index line: %r", line[:120])
    continue
```

Lines that cannot be parsed are logged and skipped rather than aborting the load, ensuring a single corrupted line does not prevent the store from loading valid historical records.

## Thread Safety

All writes go through `_append()`, which acquires a `threading.Lock` before opening the file. This prevents concurrent writes from interleaving partial JSON on the same line. The lock is per-instance, so multiple `JSONLFileStore` instances in tests do not contend with each other.

## Soft Deletes

```python
def soft_delete(self, file_id: str) -> None:
    self._deleted.add(file_id)
    self._append({"op": "delete", "id": file_id, "at": datetime.now(UTC).isoformat()})
```

Soft delete adds the ID to the in-memory `_deleted` set and appends a delete record. `get()` checks `self._deleted` first and returns `None` for deleted files, making them invisible to callers without removing them from the JSONL file.

## Datetime Serialization

The `_json_default` function handles `datetime` objects during JSON serialization by converting them to ISO 8601 strings. Without this, `json.dumps` would raise `TypeError` on `FileRecord.created`. The function raises `TypeError` for any other unknown type.

## Known Gaps

The JSONL file grows indefinitely -- there is no compaction mechanism that rewrites the file with only the current state. For deployments with high upload churn, the file could become large and slow to reload on startup. The in-memory cache is also not invalidated across multiple processes: if two PocketPaw instances share the same JSONL file, their caches will diverge after the first write.