---
{
  "title": "JSONL File Store Tests: Append-Only Metadata Persistence and Resilience",
  "summary": "This module tests `JSONLFileStore`, PocketPaw's append-only JSONL-based file metadata store. It validates save/get round-trips, soft-delete semantics, cold-reload durability, and resilience to corrupt log lines—all critical properties for a store that must survive process restarts without data loss.",
  "concepts": [
    "JSONLFileStore",
    "FileRecord",
    "append-only log",
    "soft delete",
    "cold reload",
    "JSONL",
    "metadata store",
    "file persistence",
    "corrupt line resilience",
    "log replay",
    "uploads"
  ],
  "categories": [
    "testing",
    "uploads",
    "storage",
    "metadata",
    "persistence",
    "test"
  ],
  "source_docs": [
    "5b0b36d24dac0f9a"
  ],
  "backlinks": null,
  "word_count": 454,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_file_store.py` tests `pocketpaw.uploads.file_store.JSONLFileStore`. The store is an append-only log of `FileRecord` operations serialized as newline-delimited JSON. This design is simple, portable, and crash-safe: each write is a single `fwrite` with no partial-update risk. The tests validate the store's core invariants.

## `_record` Helper

The `_record` factory function creates `FileRecord` instances with sensible defaults, allowing tests to override only the fields they care about. This keeps test code focused on what is being tested rather than record construction boilerplate.

## Test: Save/Get Round-Trip (`test_save_then_get_roundtrip`)

The most fundamental test: a record saved with `store.save(record)` must be retrievable with `store.get(file_id)`. Asserts that `filename` and `size` survive serialization and deserialization unchanged. If this test fails, no other upload functionality can work correctly.

## Test: Missing Record (`test_get_missing_returns_none`)

`store.get("nope")` on an empty store must return `None`, not raise `KeyError`. This allows API routes to distinguish "not found" from "error" without try/except on the lookup.

## Test: Soft Delete (`test_soft_delete_hides_from_get`)

`store.soft_delete("f1")` must cause `store.get("f1")` to return `None`. The JSONL log appends a delete operation rather than removing the save entry—the store replays the log and the delete wins. This matters because:

1. Rewriting the log to remove entries would require a read-modify-write cycle vulnerable to crashes.
2. The full audit trail is preserved for compliance or debugging.
3. S3 adapters can use the delete record to schedule actual blob deletion asynchronously.

## Test: Cold Reload (`test_cold_reload_preserves_state`)

This is the durability test. A store is created, records `a` and `b` are saved, `a` is soft-deleted. Then a **new** `JSONLFileStore` instance is created pointing to the same file (simulating a process restart). The new instance must:

- Return `None` for `a` (the delete survives reload).
- Return the correct record for `b` (saves survive reload).

This test proves the log-replay logic is correct and that the store can be safely used across restarts without a separate compaction step.

## Test: Corrupt Line Resilience (`test_corrupt_line_is_skipped`)

The test writes a JSONL file with three lines: a valid save, an invalid JSON line (`THIS IS NOT JSON`), and another valid save. The store must skip the corrupt line and successfully return both valid records.

This matters because:
- Power loss during a write can produce a partial JSON line.
- Log files can be manually edited by operators.

Silent skipping (with presumably a warning log) is safer than raising and making the entire store unreadable.

## Known Gaps

- No test verifies that corrupt lines are logged as warnings rather than silently skipped—a corrupt-line-swallowing bug could go unnoticed.
- No test covers very large files (thousands of records) to validate that cold-reload performance is acceptable.
- No test covers concurrent writes from two processes, which could produce interleaved lines depending on OS buffering.
