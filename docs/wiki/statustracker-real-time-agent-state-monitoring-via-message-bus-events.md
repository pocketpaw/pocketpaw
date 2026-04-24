---
{
  "title": "StatusTracker: Real-Time Agent State Monitoring via Message Bus Events",
  "summary": "`StatusTracker` subscribes to the PocketPaw message bus and maintains a live snapshot of every active agent session's current state — thinking, running a tool, streaming a response, or in an error condition. It powers the status indicator in the dashboard and supports long-poll queries so the UI can update without polling.",
  "concepts": [
    "StatusTracker",
    "SystemEvent",
    "message bus",
    "session state machine",
    "long-poll",
    "asyncio.Event",
    "version counter",
    "error TTL",
    "token accounting",
    "dashboard",
    "concurrent sessions"
  ],
  "categories": [
    "monitoring",
    "async-runtime",
    "dashboard"
  ],
  "source_docs": [
    "6947570dba7f98a5"
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

When a user submits a message to PocketPaw, they need feedback that something is happening. The `StatusTracker` provides this by listening to `SystemEvent` messages on the bus and maintaining a per-session state machine. Rather than having the agent loop push state to the UI directly, the tracker decouples the two: the agent loop emits events, the tracker consumes them and maintains a snapshot, and the API layer reads that snapshot.

## Per-Session State Machine

Each active session is tracked as a `_SessionState` dataclass with these states:

- `thinking` — default; the LLM is processing the prompt.
- `tool_running` — a tool call is in flight; `tool_name` is populated.
- `streaming` — the LLM is streaming its response tokens.
- `waiting_for_user` — the agent is waiting for additional input.
- `error` — an error occurred; `error_message` is populated.

The state machine is driven by `_on_event()`, which pattern-matches incoming `SystemEvent` payloads and transitions states accordingly.

## Error TTL and Cleanup

Errors are kept in the snapshot for `_ERROR_TTL = 30.0` seconds before being evicted. Without this, a session that errored and then became inactive would remain visible in the UI as "error" indefinitely. The TTL ensures stale errors age out automatically.

## Version-Based Change Detection

The tracker maintains a monotonically increasing `_version` integer. Every state change increments it. The `wait_for_change(since_version, timeout)` method allows callers to block until a version newer than `since_version` is observed — this is the foundation for long-poll HTTP endpoints in the dashboard API. Instead of the client polling every second, it can hold an open connection and receive a response only when state actually changes.

```python
async def wait_for_change(self, since_version: int, timeout: float) -> bool:
    if self._version > since_version:
        return True
    self._change_event.clear()
    try:
        await asyncio.wait_for(self._change_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
```

The `asyncio.Event` is used rather than a `Condition` because state changes are broadcast to all waiters simultaneously — any number of dashboard tabs can be waiting on the same tracker.

## Concurrency Cap

`max_concurrent` (default 5) caps how many sessions the tracker maintains. This prevents unbounded memory growth if many sessions are opened simultaneously. Sessions beyond the cap are evicted (oldest first) to keep the snapshot bounded.

## Token Accounting

`_SessionState` tracks `token_input` and `token_output` counts. These are populated from token-usage events emitted by the LLM backend. The `snapshot()` method includes these counts, enabling the dashboard to display running token usage for each session.

## Known Gaps

- The version counter and `asyncio.Event` are not protected by a lock. In the current single-event-loop design this is safe, but if the tracker were used across multiple threads, there would be a race between `_version` increment and `_change_event.set()`.