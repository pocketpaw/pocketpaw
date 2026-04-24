---
{
  "title": "ProactiveDaemon: Central Orchestrator for Scheduled Agent Behavior",
  "summary": "ProactiveDaemon is the top-level coordinator that wires together IntentionStore, TriggerEngine, IntentionExecutor, and ContextHub into a single lifecycle-managed service. It starts trigger scheduling on `start()`, fires intentions via `_on_trigger()` when conditions are met, and tears down cleanly on `stop()`.",
  "concepts": [
    "ProactiveDaemon",
    "orchestration",
    "TriggerEngine",
    "IntentionStore",
    "IntentionExecutor",
    "ContextHub",
    "lifecycle management",
    "scheduler",
    "fire-and-forget",
    "run_intention_now"
  ],
  "categories": [
    "Daemon",
    "Orchestration"
  ],
  "source_docs": [
    "5f34251983b14883"
  ],
  "backlinks": null,
  "word_count": 405,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ProactiveDaemon` in `src/pocketpaw/daemon/proactive.py` is the glue layer for PocketPaw's proactive behavior system. Its role is purely orchestration: it doesn't implement scheduling logic (that's `TriggerEngine`), doesn't persist data (that's `IntentionStore`), doesn't gather context (that's `ContextHub`), and doesn't invoke the LLM (that's `IntentionExecutor`). Instead it wires all of these together and manages their shared lifecycle.

## Why a Separate Daemon Class?

Without an orchestrator, the dashboard startup code would need to know about every subsystem's initialization order and wiring. By encapsulating this in `ProactiveDaemon`, the dashboard calls `daemon.start(stream_callback=...)` and gets a fully operational proactive system. This also makes the daemon testable in isolation: tests can construct a daemon with mock components without spinning up a full server.

## Lifecycle

### start()

`start()` is the main initialization path:

1. Sets `_started = True` and registers the stream callback with `IntentionExecutor`.
2. Calls `trigger_engine.start(callback=self._on_trigger)` — this starts the APScheduler and registers all enabled intentions as jobs.
3. Calls `_schedule_all_intentions()` which iterates `intention_store.get_enabled()` and calls `trigger_engine.add_intention()` for each one.

The idempotency guard (`if self._started: return`) prevents double-start if the dashboard's lifespan event fires twice (which can happen during development reloads).

### _on_trigger()

When a trigger fires, `TriggerEngine` calls `_on_trigger(intention)`. The daemon then calls `executor.execute_and_stream(intention)`, which gathers context, templates the prompt, invokes the agent, and streams the result through the registered callback. This is an async fire-and-forget via `asyncio.create_task()` to prevent blocking the scheduler.

### stop()

Calls `trigger_engine.stop()` which shuts down APScheduler and removes all registered jobs. Also sets `_started = False`.

## CRUD Delegation

`ProactiveDaemon` exposes intention management methods (`get_intentions()`, `create_intention()`, `delete_intention()`) that delegate to `IntentionStore` and then update the trigger engine accordingly. When an intention is created, it's both persisted and immediately scheduled. When deleted, it's removed from persistence and unscheduled. This keeps the store and engine in sync.

## run_intention_now()

This method bypasses the normal scheduling path and immediately executes an intention by ID. It calls `executor.execute_and_stream()` directly, triggering the full context-gather-template-invoke pipeline. The dashboard exposes this as a "Run Now" button.

## reload_intentions()

If the user edits `intentions.json` directly, `reload_intentions()` re-reads from disk, removes all existing APScheduler jobs, and re-registers all enabled intentions. This provides a manual sync mechanism.

## Known Gaps

- There is no watch-based auto-reload. If `intentions.json` is edited externally, `reload_intentions()` must be called explicitly (e.g., via the dashboard API).
- Error handling in `_on_trigger()` is minimal — if `execute_and_stream()` raises an unhandled exception, it will surface as an unhandled task exception.