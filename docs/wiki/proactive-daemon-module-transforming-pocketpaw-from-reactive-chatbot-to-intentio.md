---
{
  "title": "Proactive Daemon Module: Transforming PocketPaw from Reactive Chatbot to Intention-Driven Agent",
  "summary": "The `pocketpaw.daemon` package turns PocketPaw from a request/response chatbot into a proactive agent that initiates actions based on user-defined intentions and event triggers. Its `__init__.py` assembles the five subsystems — `IntentionStore`, `TriggerEngine`, `ContextHub`, `IntentionExecutor`, and `ProactiveDaemon` — into a single importable surface.",
  "concepts": [
    "ProactiveDaemon",
    "IntentionStore",
    "TriggerEngine",
    "ContextHub",
    "IntentionExecutor",
    "proactive agent",
    "intentions",
    "background daemon",
    "reactive vs proactive",
    "event-driven"
  ],
  "categories": [
    "daemon",
    "architecture",
    "agent behavior"
  ],
  "source_docs": [
    "0c1370bedf53cf07"
  ],
  "backlinks": null,
  "word_count": 443,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/daemon/__init__.py` is the entry-point for PocketPaw's proactive daemon system. The module docstring frames its purpose clearly: it "transforms PocketPaw from a reactive chatbot into a proactive AI agent that initiates actions based on user-defined 'intentions' and various triggers."

## The Reactive vs. Proactive Distinction

Most AI assistants are purely reactive — they wait for the user to send a message, then respond. PocketPaw's daemon breaks this pattern by letting the agent act on schedules, events, and conditions without waiting for user input. Examples:

- Send a daily digest of unread emails at 8 AM
- Alert the user when a monitored GitHub issue is closed
- Summarise Slack messages from a channel when the user has been idle for 2 hours

This is architecturally significant: it means PocketPaw needs a long-running process (the daemon) separate from the request-handling server, with its own concurrency model and failure-recovery logic.

## Five Subsystems

### `IntentionStore` + `get_intention_store`

Persists user-defined intentions — standing instructions the agent should act on when conditions are met. An intention is roughly: "when X happens, do Y." `get_intention_store` is a factory/singleton accessor that avoids passing the store through every call stack.

### `TriggerEngine`

Monitors trigger conditions: time schedules (cron-style), external events (webhooks), polling intervals, and user-defined conditions. When a trigger fires, it looks up matching intentions and queues them for execution.

### `ContextHub`

Aggregates context that the executor needs when running an intention: recent conversation history, current calendar state, active notifications, connector data. The hub is separate from the executor so context assembly can be tested independently.

### `IntentionExecutor`

Runs a queued intention by constructing a prompt from the intention definition and current context, dispatching it to the LLM, and executing any tool calls the LLM produces. It handles retries, timeout enforcement, and result persistence.

### `ProactiveDaemon` + `get_daemon`

The top-level coordinator that starts the trigger engine, wires it to the executor, and manages the background event loop. `get_daemon` is the singleton accessor used by the FastAPI lifespan handler to start/stop the daemon with the server.

## Why a Separate Package?

The daemon subsystems have a different concurrency model than the request-handling server (long-running loops vs. per-request async handlers). Isolating them in `pocketpaw.daemon` prevents accidental import of async background tasks by modules that run in the request path, and makes the daemon independently testable.

## Known Gaps

- The module doc and exports are defined, but the concrete implementations in `context.py`, `executor.py`, `intentions.py`, `proactive.py`, and `triggers.py` are not included in this batch — their full implementation details are not surfaced here.
- There is no documented strategy for daemon persistence across server restarts (e.g., recovering in-flight intentions after a crash).
