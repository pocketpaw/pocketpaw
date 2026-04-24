---
{
  "title": "AgentLoop Concurrency Controls: Session Locks, Global Semaphore, and File Memory Safety",
  "summary": "This test module validates the three-layer concurrency model in PocketPaw's AgentLoop: per-session serialization via asyncio locks, cross-session parallelism as the happy path, and a global semaphore that caps total simultaneous conversations. It also covers the FileMemoryStore's write lock and transient PermissionError retry logic that prevent JSON corruption under concurrent writes.",
  "concepts": [
    "AgentLoop",
    "session lock",
    "asyncio.Lock",
    "global semaphore",
    "max_concurrent_conversations",
    "FileMemoryStore",
    "PermissionError retry",
    "cross-session parallelism",
    "InboundMessage",
    "concurrency control",
    "asyncio.gather"
  ],
  "categories": [
    "concurrency",
    "testing",
    "agent runtime",
    "memory",
    "test"
  ],
  "source_docs": [
    "640d7049a88fbecb"
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

PocketPaw's `AgentLoop` must handle messages from many users at once without letting concurrent coroutines corrupt shared state. This test file proves three distinct concurrency guarantees that together form a layered defense: per-session ordering, cross-session parallelism, and a global throughput cap.

## Layer 1: Per-Session Lock (Serialization)

`test_session_lock_serialises_same_session` fires two messages with the **same** `session_key` concurrently using `asyncio.gather`. The test injects a slow mock router (50 ms delay) and records a timeline of start/end events. The assertion `order.index("end:first") < order.index("start:second")` proves the second message didn't begin processing until the first completed.

Without this lock, a fast second message could interleave with a slow first one, causing the memory manager to read a half-written session history and produce incoherent agent responses. The per-session `asyncio.Lock` prevents that race without blocking unrelated users.

## Layer 2: Cross-Session Parallelism

`test_cross_session_runs_in_parallel` sends messages from two distinct session keys (`userA`, `userB`) concurrently. The assertion checks that both start events precede the first end event — confirming genuine overlap, not serial execution.

This matters because naive implementations might accidentally serialize everything (e.g., a module-level lock). The test exists to catch regressions where a developer introduces a broad lock that kills throughput for unrelated users.

## Layer 3: Global Semaphore (`max_concurrent_conversations`)

`test_global_semaphore_caps_concurrency` sets `max_concurrent_conversations=1`, which forces even different-session messages to run serially. The assertion confirms that the first message fully finishes before the second starts.

The setting defaults to `5` (confirmed in `test_config_max_concurrent_conversations_default`). This cap protects the host from resource exhaustion when a spike of users hits simultaneously. Without it, an unbounded number of concurrent LLM calls could exhaust file descriptors, memory, or API rate limits.

The `test_config_max_concurrent_conversations_save` test ensures the field round-trips through `Settings.save()` so it persists across restarts — a deployment concern, not just a runtime one.

## FileMemoryStore: Write Lock and Retry

The file-backed memory store writes session entries as JSON files. `test_file_memory_store_session_lock` fires 10 concurrent `_save_session_entry` calls for the same session and then asserts all 10 entries are present in valid JSON. Without a per-session `asyncio.Lock`, concurrent coroutines could interleave their read-modify-write cycles, causing some entries to be silently dropped or the file to contain malformed JSON.

`test_file_memory_store_permission_error_retry` simulates a transient `PermissionError` on the atomic `Path.replace()` call (the move from `.tmp` to the final file). On Windows and some Linux configurations, file locks or antivirus scanners briefly hold files open, causing exactly this error. The store retries the replace, and the test confirms the entry lands correctly after one failure.

## Helper Infrastructure

`_make_inbound` constructs minimal `InboundMessage` objects bound to the WebSocket channel. `_make_slow_router` wraps an async generator with a configurable `asyncio.sleep` delay, giving tests precise control over timing without real I/O.

The heavy use of `@patch` decorators on `AgentLoop` dependencies (`get_settings`, `AgentContextBuilder`, `get_memory_manager`, `get_message_bus`, `get_injection_scanner`) ensures the concurrency behavior is tested in isolation without triggering real LLM calls, file I/O, or event bus side effects.

## Known Gaps

No TODO or FIXME markers were found. The semaphore tests skip numbered sections (3 exists, but 4 jumps to 7 for the config tests) — sections 5 and 6 are absent in the committed file, suggesting those test cases may have been planned but not yet written.