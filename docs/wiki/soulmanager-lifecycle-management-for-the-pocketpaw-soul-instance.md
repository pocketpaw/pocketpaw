---
{
  "title": "SoulManager: Lifecycle Management for the PocketPaw Soul Instance",
  "summary": "`SoulManager` is PocketPaw's singleton that owns the entire lifecycle of a `.soul` file — initialization, concurrency-safe observation, periodic auto-save, external change detection, and graceful shutdown. It is the integration seam between PocketPaw's real-time agent sessions and the persistent soul-protocol memory system.",
  "concepts": [
    "SoulManager",
    "asyncio.Lock",
    "auto-save",
    "soul lifecycle",
    "corrupt file recovery",
    "external sync",
    "CognitiveEngine",
    "observe",
    "mtime",
    "singleton",
    "BaseTool",
    "biorhythms"
  ],
  "categories": [
    "soul-protocol",
    "persistence",
    "async-runtime"
  ],
  "source_docs": [
    "8dac750adafa0be9"
  ],
  "backlinks": null,
  "word_count": 528,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A PocketPaw soul is a long-lived object that accumulates memories across many user sessions. `SoulManager` exists to manage that object safely in an async, multi-request environment where several concerns collide: file I/O must not block the event loop, concurrent observations from rapid user messages must be serialized, and a crash should not lose recent interactions.

## Initialization and Corrupt File Handling

`initialize()` loads the `.soul` file from `settings.soul_dir`. If the file is missing, it births a fresh soul. If the file exists but is unreadable (corrupt, encrypted with a lost key, or from an incompatible format version), the manager does not raise — it backs up the broken file with a timestamp suffix and births a fresh soul. This prevents a single bad `.soul` file from permanently breaking the agent for a user.

## Concurrency Safety via asyncio.Lock

The `observe()` method is the hot path: it is called after every user/agent exchange to record the interaction in memory. Because rapid back-and-forth messages can trigger multiple concurrent `observe()` calls, an `asyncio.Lock` serializes them. Without this lock, two concurrent observations could corrupt the soul's internal memory state (which maintains ordered memory tiers and relevance scores).

## Periodic Auto-Save

A background task runs `_auto_save_loop()` on a configurable interval (default 5 minutes). This protects against data loss if the Python process is killed with SIGKILL — a graceful `shutdown()` may never run in that scenario. The auto-save task is started via `start_auto_save()` and cancelled in `shutdown()`.

## External Change Detection

`_file_changed_externally()` compares the current mtime of the `.soul` file against the recorded mtime from the last save. If a change is detected (e.g., the user ran `soul remember` from the CLI while the agent was running), `reload()` re-reads the file. This is the mechanism behind the "Soul Sync" feature in the OCEAN workspace — CLI writes to the soul file are picked up by the running PocketPaw agent automatically.

## CognitiveEngine Wiring

`initialize()` accepts an optional `engine` parameter that is passed to the soul instance. When a `PocketPawCognitiveEngine` is provided, the soul uses PocketPaw's own LLM backends for fact extraction and reflection instead of heuristic fallbacks. This wiring point is where the cognitive bridge is connected at startup.

```python
async def initialize(self, engine=None) -> None:
    try:
        soul = SoulClass.load(self.soul_file)
    except Exception:
        self._backup_corrupt_file()
        soul = await self._birth_soul(SoulClass, engine)
    if engine:
        soul.set_cognitive_engine(engine)
    self._soul = soul
    self._record_file_mtime()
    self.start_auto_save()
```

## Import Tool Integration

`get_tools()` returns a list of `BaseTool` instances that expose soul operations (remember, recall, status) to the agent. This makes the soul queryable from within a conversation — the agent can ask "what do I know about this user?" and get a grounded answer from memory.

## Singleton Reset for Tests

`_reset_manager()` is a module-level function that clears the singleton reference. It exists purely for test isolation — without it, a soul loaded in one test would leak into subsequent tests.

## Known Gaps

- **Auto-sync polling** (v0.2.4+) relies on mtime comparison, which has 1-second granularity on some filesystems. Very rapid external writes within the same second may be missed.
- **Biorhythm and rubric evaluation** are described in the module docstring but their configuration surface (`DNA` parameters) is not documented externally.