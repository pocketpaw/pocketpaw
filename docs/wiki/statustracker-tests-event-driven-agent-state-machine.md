---
{
  "title": "StatusTracker Tests: Event-Driven Agent State Machine",
  "summary": "Tests for `StatusTracker`, which maintains real-time agent state by consuming `SystemEvent` objects from the internal event bus. Covers state transitions from idle through thinking, tool execution, and error states, plus multi-session concurrency and token usage accumulation.",
  "concepts": [
    "StatusTracker",
    "SystemEvent",
    "session lifecycle",
    "state transitions",
    "degraded state",
    "token accumulation",
    "multi-session",
    "agent_start",
    "agent_end",
    "waiting_for_human"
  ],
  "categories": [
    "testing",
    "monitoring",
    "event system",
    "test"
  ],
  "source_docs": [
    "df959ada504efc0e"
  ],
  "backlinks": null,
  "word_count": 401,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`StatusTracker` is the component that translates PocketPaw's internal event stream into a queryable snapshot of agent state. It is the data source for the `/status` API and the CLI monitor. Because it is purely event-driven, its tests work by injecting `SystemEvent` objects directly and asserting on the resulting `snapshot()` output.

## Default State

`test_idle_by_default` confirms that a newly created tracker is in `idle` state with zero active sessions and an empty sessions list. This is the baseline from which all state transitions depart.

## Session Lifecycle

`test_agent_start_creates_session` is foundational: an `agent_start` event must create a session entry with the correct `session_key`, `channel`, and `session_id`. The `session_key` format `"channel:id"` is parsed into its components, enabling per-channel reporting on the dashboard.

`test_agent_end_removes_session` verifies the inverse: an `agent_end` event removes the session from the snapshot. Without this, terminated sessions would accumulate indefinitely, inflating the `active_sessions` count and misrepresenting system load.

## State Transitions

Six state-transition tests cover the major agent phases:

- `test_thinking_state`: The `thinking` event sets session state to `thinking`.
- `test_tool_running_state`: A `tool_use` event marks the session as running a tool.
- `test_tool_result_transitions_to_streaming`: After a tool result arrives, the session transitions to streaming (generating the final response).
- `test_error_state_sets_degraded`: An `error` event sets the global `degraded` flag. This flag signals to operators and dashboards that intervention may be needed.
- `test_waiting_for_user_state`: A `waiting_for_human` event sets the appropriate state, distinguishing "paused waiting for user input" from "idle".

## Token Accumulation

`test_token_usage_accumulates` verifies that token counts from multiple events are summed correctly. This drives the token usage display in the dashboard and is used for cost tracking.

## Concurrency Metrics

`test_max_concurrent_in_snapshot` confirms the snapshot includes the `max_concurrent` capacity the tracker was initialised with. Clients use this to render capacity bars.

`test_multiple_sessions` runs two independent sessions concurrently and verifies both appear correctly in the snapshot without interfering with each other.

`test_degraded_with_mixed_states` tests a scenario where one session is healthy and another is in error — the global `degraded` flag must be `true` even though not all sessions are failing.

## Event Guard

`test_ignores_events_without_session_key` confirms the tracker silently drops events that lack a `session_key`. This prevents crashes from malformed events published by tools or external adapters that omit the session context.

## Known Gaps

No token-usage breakdown by session (only global accumulation is tested). Future work may require per-session cost tracking for multi-tenant deployments.

```python
# Tracker fixture
@pytest.fixture
def tracker():
    return StatusTracker(max_concurrent=3)
```
