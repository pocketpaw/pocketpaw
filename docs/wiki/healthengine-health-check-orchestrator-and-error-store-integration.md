---
{
  "title": "HealthEngine — Health Check Orchestrator and Error Store Integration",
  "summary": "`HealthEngine` is PocketPaw's central health orchestration class, running startup config/storage checks synchronously and connectivity checks asynchronously, storing results in memory and routing check failures to the persistent `ErrorStore`. It is deliberately LLM-independent so the health system remains operational even when the AI backend is down.",
  "concepts": [
    "HealthEngine",
    "health orchestration",
    "startup checks",
    "connectivity checks",
    "ErrorStore",
    "prompt injection",
    "LLM independence",
    "async checks",
    "error recording",
    "STARTUP_CHECKS",
    "CONNECTIVITY_CHECKS"
  ],
  "categories": [
    "health monitoring",
    "architecture"
  ],
  "source_docs": [
    "b7d2fd07ed2219ad"
  ],
  "backlinks": null,
  "word_count": 520,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`HealthEngine` is the single entry point for all health check coordination in PocketPaw. It decouples check *execution* (calling individual check functions) from check *consumption* (the dashboard UI, prompt injection, error store). The class is pure Python with no LLM dependency, which is a deliberate design decision: if the AI backend fails, the health engine must still be able to diagnose why.

## Initialization

```python
def __init__(self):
    self._results: list[HealthCheckResult] = []
    self._error_store = ErrorStore()
    self._last_check: str = ""
```

The engine holds results in memory (`_results`) for fast access by the dashboard, backed by `ErrorStore` for persistence across page refreshes and server restarts.

## Two Check Phases

### Startup Checks (Synchronous)

`run_startup_checks()` iterates `STARTUP_CHECKS` — a list of synchronous check functions for config validation and storage — and calls each one directly.

```python
for check_fn in STARTUP_CHECKS:
    try:
        result = check_fn()
        results.append(result)
    except Exception as e:
        results.append(HealthCheckResult(
            check_id=check_fn.__name__.replace("check_", ""),
            status="warning",
            message=f"Check itself failed: {e}",
        ))
```

The `except` block ensures a buggy check function never aborts the entire startup sequence. The failed check is reported as `warning` with the exception message, so the operator can see what went wrong.

The final `self._last_check = datetime.now(tz=UTC).isoformat()` timestamp is stored for display in the health dashboard ("Last checked 3 minutes ago").

### Connectivity Checks (Asynchronous)

`run_connectivity_checks()` is `async` and iterates `CONNECTIVITY_CHECKS`, awaiting each one. These checks involve network I/O (pinging LLM APIs) and must not block the server's event loop. The same defensive exception pattern applies.

After running, connectivity results are merged into `self._results` alongside the startup results so the full picture is available in one list.

## Prompt Injection

The engine provides a `build_health_prompt_context()` method (visible in the full source) that formats the current check results as a compact text block suitable for injection into an LLM system prompt. This lets the agent know its own health state — for example, if the disk is nearly full or an API key is missing, the agent can proactively inform the user rather than silently failing.

## Error Recording

```python
def record_error(self, message, source="unknown", severity="error", traceback="", context=None):
    return self._error_store.record(message, source, severity, traceback, context)
```

`record_error()` is a thin pass-through to `ErrorStore`. The engine provides it as a stable public API so other modules (API routes, tool handlers) can record errors through the engine without needing to import `ErrorStore` directly — centralising the error recording entry point.

## LLM Independence

The class docstring makes this explicit:

> Pure Python, no LLM dependency. The agent is a *consumer* of health state. If the LLM is down, the health engine still works.

This design means health diagnostics can run before any model connection is established, which is exactly when they are most needed (first-run setup, API key debugging, network issues).

## Known Gaps

- `run_connectivity_checks()` runs checks sequentially with `await` rather than in parallel with `asyncio.gather()`. For users with multiple backends configured, this means connectivity checks are slower than necessary.
- The merge of connectivity results into `self._results` replaces the previous connectivity results but does not cleanly separate startup results from connectivity results — a full re-run of startup checks is required to refresh that portion.
