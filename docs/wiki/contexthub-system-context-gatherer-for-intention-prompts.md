---
{
  "title": "ContextHub: System Context Gatherer for Intention Prompts",
  "summary": "ContextHub collects real-time system information — CPU, memory, disk, battery, current time, and health engine status — and injects it into intention prompt templates via `{{variable}}` placeholders. It acts as the sensory layer that allows PocketPaw's proactive intentions to be environment-aware rather than static.",
  "concepts": [
    "ContextHub",
    "context gathering",
    "intention prompts",
    "template variables",
    "psutil",
    "system status",
    "caching TTL",
    "health status",
    "singleton pattern",
    "proactive daemon"
  ],
  "categories": [
    "Daemon",
    "Context Management"
  ],
  "source_docs": [
    "8498cfc40aaf90d8"
  ],
  "backlinks": null,
  "word_count": 581,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ContextHub` lives in `src/pocketpaw/daemon/context.py` and solves a core problem with proactive AI prompts: static text is useless when you need the agent to react to real conditions. A morning standup prompt that says "what are your priorities?" is fine, but one that also knows the user's system is at 95% memory might trigger a useful cleanup suggestion first. ContextHub is the bridge between the raw OS environment and the language model.

## Context Sources

ContextHub defines a fixed list of `AVAILABLE_SOURCES`:

- **`system_status`** — CPU percent, memory (used/available/total), disk usage, and optional battery status gathered via `psutil`. The battery section is conditionally included only when `psutil.sensors_battery()` returns a result, preventing `None` errors on desktop machines without batteries.
- **`datetime`** — Current UTC date, time, day of week, ISO string, and Unix timestamp. This is always cheap to gather.
- **`health_status`** — Pulls from the health engine's `summary` property. Added in 2026-02-17 to give intentions visibility into whether the agent runtime itself is degraded before firing a prompt.
- **`active_window`** — Commented out, marked as Phase 2. This would expose the currently focused application to enable context-aware responses (e.g., "I see you're in VSCode — want a code review?").

## Caching Strategy

All context sources are cached with a 5-second TTL (`_cache_ttl = 5`). The cache stores `(datetime, value)` tuples keyed by source name. This exists because multiple intentions may fire in the same scheduling tick, and calling `psutil.cpu_percent(interval=0.1)` repeatedly within seconds would add latency and CPU overhead for no benefit. The 5-second window is short enough to remain relevant while preventing redundant system calls.

## Defensive Error Handling

Each source gather is wrapped in a `try/except` inside `gather()`. If a source fails — for example, if `psutil` raises an exception on an unusual platform — the error is recorded as a string in the context dict rather than propagating the exception. This means a prompt will receive `"Error: ..."` for that context field rather than causing the entire intention execution to abort.

The `system_status` gatherer has an additional guard: it catches `ImportError` from `psutil` explicitly and returns a degraded dict with a human-readable install hint. This matters because `psutil` is an optional dependency (part of `pocketpaw[desktop]`), and users who install the base package should get a clear error message rather than a cryptic traceback.

## Template Variable Substitution

`apply_template()` handles three layers of substitution:

1. `{{context}}` — replaced with the full formatted string of all gathered context.
2. `{{source_name}}` — replaced with the formatted string for a specific source (e.g., `{{system_status}}`).
3. Dot-notation paths like `{{system_status.cpu_percent}}` — resolved via `_get_nested_value()` which walks nested dicts using `str.split(".")`.

The regex pattern for matching placeholders (`{{variable}}`) covers only alphanumeric and underscore names with optional dot segments, preventing injection of arbitrary text through template variables.

## Singleton Pattern

`get_context_hub()` returns a module-level singleton. Since `ContextHub` holds an in-memory cache, sharing one instance across all intention executions ensures the TTL-based deduplication actually works — each new instance would have an empty cache, defeating the purpose.

## Known Gaps

- **`active_window` and `recent_files`** are stubbed out as Phase 2 features. The gatherer dispatch table has commented-out entries for these, meaning they will silently return `None` if accidentally referenced.
- The `_gather_active_window` and `_gather_recent_files` methods are not implemented — calling them via `_gather_source()` would hit the `return None` fallback path.
- The cache is not thread-safe. Since PocketPaw's daemon uses `asyncio`, this is acceptable in practice, but concurrent coroutines could theoretically race on the cache dict.