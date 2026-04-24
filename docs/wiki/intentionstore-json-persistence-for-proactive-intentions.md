---
{
  "title": "IntentionStore: JSON Persistence for Proactive Intentions",
  "summary": "IntentionStore manages the CRUD lifecycle for user-defined intentions stored as a JSON file in `~/.pocketpaw/intentions.json`. Each intention records what prompt to run, when to run it (trigger), and which context sources to include.",
  "concepts": [
    "IntentionStore",
    "intentions",
    "JSON persistence",
    "CRUD",
    "proactive behavior",
    "trigger scheduling",
    "singleton pattern",
    "enabled/disabled",
    "mark_run",
    "UUID"
  ],
  "categories": [
    "Daemon",
    "Data Persistence"
  ],
  "source_docs": [
    "5c2965a162bdc5b8"
  ],
  "backlinks": null,
  "word_count": 516,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`IntentionStore` in `src/pocketpaw/daemon/intentions.py` is the persistence layer for PocketPaw's proactive intention system. Intentions are the user-defined rules that tell the agent what to do and when — for example, "send me a standup prompt every weekday at 8am" or "check in when a browser session has been idle for 12 hours".

## Storage Format

Intentions are stored in `~/.pocketpaw/intentions.json`. Each intention follows this schema:

```python
{
    "id": "uuid",
    "name": "Morning Standup",
    "prompt": "Good morning! What are your top 3 priorities today?",
    "trigger": {"type": "cron", "schedule": "0 8 * * 1-5"},
    "context_sources": ["system_status"],
    "enabled": true,
    "created_at": "ISO timestamp",
    "last_run": "ISO timestamp or null"
}
```

The JSON file also stores an `updated_at` timestamp at the top level, written on every save. This can help detect external modifications and is available for future sync features.

## Why JSON, Not SQLite?

The choice of plain JSON over a database is deliberate. The intention count for a typical user is small (single digits to low tens), the data is simple key-value structures, and JSON files are human-readable and portable. A user can hand-edit `intentions.json` to add an intention without going through the dashboard. SQLite would add complexity, migration overhead, and a binary format that's opaque to users inspecting their own data directory.

## Error-Tolerant Loading

`load_intentions()` catches both `OSError` and `json.JSONDecodeError`. If the file is missing, it returns an empty list (normal first-run state). If the file is corrupt — for example, truncated during a write — it logs the error and returns an empty list rather than crashing. This is intentional: a corrupt intentions file should not prevent the rest of the daemon from starting.

## CRUD Operations

- **`get_all()`** returns a copy of the in-memory list. The copy prevents callers from accidentally mutating the store's internal state.
- **`get_enabled()`** filters to intentions where `enabled` is `True`. Newly created intentions have `enabled=True` by default, but users can disable them without deleting them.
- **`create()`** generates a UUID v4 for the ID, stamps `created_at`, sets `last_run=None`, appends to the in-memory list, and immediately persists to disk. The UUID ensures stable IDs even if intentions are reordered.
- **`delete()`** removes by ID and persists. Returns `False` if the ID is not found, allowing callers to detect double-delete attempts.
- **`mark_run()`** updates `last_run` to the current UTC timestamp and persists. This is called by `IntentionExecutor` after a successful execution so the dashboard can display "last run" times.
- **`reload()`** re-reads from disk. This is useful after external edits to the JSON file.

## Singleton Pattern

`get_intention_store()` returns a module-level singleton. Since the store holds an in-memory copy of all intentions, sharing one instance ensures that changes made through the dashboard API are immediately visible to the trigger engine without requiring a disk round-trip.

## Known Gaps

- There is no file-locking on `save_intentions()`. If two coroutines write simultaneously (unlikely in the asyncio model, but possible with threads), the file could be corrupted. A `tempfile`-rename pattern would be safer.
- The `update()` method is not implemented. To modify an existing intention (e.g., change the schedule), callers must delete and recreate it.