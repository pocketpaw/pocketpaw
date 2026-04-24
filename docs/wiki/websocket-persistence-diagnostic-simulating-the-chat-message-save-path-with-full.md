---
{
  "title": "WebSocket Persistence Diagnostic: Simulating the Chat Message Save Path with Full Debug Logging",
  "summary": "This script simulates the exact code path the WebSocket adapter executes when saving a chat message, but with DEBUG-level logging and no exception swallowing. It was created to surface silent persistence failures that were invisible in normal operation because the WebSocket handler caught and discarded exceptions.",
  "concepts": [
    "WebSocket persistence",
    "silent failure",
    "DEBUG logging",
    "init_cloud_db",
    "_ensure_cloud_session",
    "Message insert",
    "Beanie",
    "exception swallowing",
    "chat persistence",
    "Motor",
    "paw-enterprise"
  ],
  "categories": [
    "scripts",
    "diagnostics",
    "memory",
    "websocket"
  ],
  "source_docs": [
    "6807100b50ade97c"
  ],
  "backlinks": null,
  "word_count": 499,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scripts/diag_save_message.py` exists specifically because a class of persistence bugs was invisible in normal operation. The WebSocket adapter's message save path caught exceptions broadly to keep the connection alive, meaning messages could silently fail to persist without any user-visible error. This script reproduces the same code path with the exception handling stripped away and logging turned up to DEBUG, so any failure surfaces immediately.

## The Problem It Solves

The module docstring states the motivation: "surface any silent error". In production, the WebSocket handler prioritizes keeping the connection alive over reporting persistence failures — a reasonable trade-off for user experience but a debugging nightmare. This script inverts that priority: it re-runs the same persistence operations in a context where exceptions propagate and every log message is visible.

## Step-by-Step Execution

### Step 1: Initialize the Cloud Database
```python
await init_cloud_db("mongodb://localhost:27017/paw-enterprise")
```
This mirrors the startup sequence of the enterprise backend. If the database is unavailable or the connection string is wrong, this step fails loudly — in the WebSocket path, this failure might have been swallowed.

### Step 2: Ensure Cloud Session
```python
info = await _ensure_cloud_session("diag-chat-1")
```
`_ensure_cloud_session()` is the function that creates or retrieves a session record for a given chat ID. The diagnostic checks its return value explicitly — if it returns `None`, the script exits with code `2` and prints a clear message: "persistence will be skipped". This is the exact condition that caused silent failures in early versions: `_ensure_cloud_session()` returned `None` and the caller silently skipped all persistence.

### Step 3: Save a Message
```python
msg = Message(group=info["group_id"], sender=info["user_id"], ...)
await msg.insert()
```
This inserts a Message document directly via Beanie, exactly as the WebSocket handler does. The `msg.id` is printed after insert to confirm the document was assigned an ID by MongoDB.

### Step 4: Verify in DB
The script queries back the message by `group_id` and prints all messages in the group. This closes the loop: write → verify the write actually landed in the database.

## DEBUG Logging

```python
logging.basicConfig(level=logging.DEBUG, ...)
```
All four of PocketPaw's persistence layers (Motor, Beanie, the session helper, the message model) emit DEBUG logs. With this logging level, connection pool events, query serialization, and index operations are all visible — providing a complete trace of what happened during the save.

## Exit Codes

- `0`: All four steps completed successfully
- `1`: Unhandled exception (the script intentionally does not catch)
- `2`: `_ensure_cloud_session()` returned `None`

The exit code of `2` is particularly useful: it distinguishes "session creation failed" from "message insert failed", which have different root causes and different fixes.

## Known Gaps

- The script hardcodes `"mongodb://localhost:27017/paw-enterprise"` rather than reading from `POCKETPAW_CLOUD_MONGO_URI`; this is inconsistent with the other diagnostic scripts.
- The `diag-chat-1` chat ID is not unique between runs — repeated runs may interact with session data from previous runs.
- Only the user message save path is tested; the assistant message path uses a slightly different code route and is not covered.