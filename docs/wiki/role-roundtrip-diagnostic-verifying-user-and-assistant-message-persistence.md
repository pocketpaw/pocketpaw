---
{
  "title": "Role Roundtrip Diagnostic: Verifying User and Assistant Message Persistence",
  "summary": "A targeted diagnostic script that writes four messages with alternating `user` and `assistant` roles to MongoDB via the memory manager, then reads them back to verify that roles are persisted and retrieved correctly. It was created to debug a specific class of bug where the agent loop was saving messages but role information was being lost or corrupted during the roundtrip.",
  "concepts": [
    "role roundtrip",
    "message persistence",
    "Beanie",
    "MemoryFactDoc",
    "memory manager",
    "bus key",
    "session key",
    "key translation",
    "asyncio",
    "cloud backend",
    "register_default_backend",
    "sender_type"
  ],
  "categories": [
    "scripts",
    "diagnostics",
    "memory",
    "database"
  ],
  "source_docs": [
    "dfb8755435397af1"
  ],
  "backlinks": null,
  "word_count": 487,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scripts/diag_role_roundtrip.py` targets a narrow but critical correctness property: when the agent loop saves a `user` message and an `assistant` message, do both come back with the correct `role` field when read from MongoDB? This matters because a corrupted role roundtrip causes the LLM context to be reconstructed incorrectly — the model would see its own outputs labeled as user messages, breaking the conversation.

## Why This Diagnostic Exists

The module docstring states the motivation directly: "agent-loop saves both user and assistant — verify roles round-trip." This implies there was a period where this was not working — either the `role` field was not being persisted, was being overwritten, or the read path was normalizing it away. The diagnostic was written to provide a reproducible, inspectable test case without requiring a full UI session.

## Isolated Database

The script creates a unique database name for each run:
```python
db_name = f"diag_role_{uuid.uuid4().hex[:8]}"
uri = f"mongodb://localhost:27017/{db_name}"
```
This isolation prevents the diagnostic from contaminating the development database and ensures each run starts from a clean state. The throwaway database is not cleaned up after the run — this is intentional, allowing the developer to inspect the raw documents after the script exits.

## Stack Initialization

The script initializes Beanie with both the application document models (`ALL_DOCUMENTS`) and `MemoryFactDoc`, then calls `register_default_backend()`. This mirrors exactly what the enterprise cloud backend does at startup — ensuring the diagnostic exercises the same code paths as production, not a simplified standalone implementation.

## Key Symmetry Check

A subtle but important aspect is the key translation between the memory manager's bus key and the Message model's session key:
```python
bus_key = f"websocket:{chat_id}"
ui_key  = f"websocket_{chat_id}"
```
The bus key uses `:` as the separator; the Message model uses `_`. The diagnostic explicitly prints both keys and queries using `ui_key`, exposing this translation layer. If a bug existed in this translation, the query would return zero results even though messages were written.

## What Gets Printed

After writing four messages (user, assistant, user, assistant), the script prints the raw MongoDB documents including `role`, `sender_type`, and message ID. A correct run shows:
```
id=... role='user' sender_type='user' content='hi user 1'
id=... role='assistant' sender_type='assistant' content='hi assistant 1'
...
```
Any deviation — wrong role, missing field, wrong order — is immediately visible without needing a debugger.

## Memory Manager Verification

The script also prints `type(manager._store).__name__` immediately after `get_memory_manager()`. This verifies that `register_default_backend()` actually changed the store to the cloud backend, not left it as the default in-memory store. A frequent source of confusion during enterprise backend development was accidentally running with the wrong backend.

## Known Gaps

- The throwaway database is never cleaned up — repeated runs create multiple `diag_role_*` databases that accumulate in the local MongoDB instance.
- The script does not test the retrieval path via `get_session_history()` — it only verifies raw document presence. A bug in the read path would not be caught.