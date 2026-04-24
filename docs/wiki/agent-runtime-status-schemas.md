---
{
  "title": "Agent Runtime Status Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's agent status API, covering per-session execution states and global runtime health. The session model captures granular execution phase (thinking, tool running, streaming) alongside token usage and duration, giving operators deep visibility into what the agent is doing at any moment.",
  "concepts": [
    "SessionStatus",
    "GlobalStatus",
    "AgentStatusResponse",
    "agent state machine",
    "session_key",
    "session_id",
    "token_usage",
    "tool_running",
    "concurrency",
    "Pydantic alias"
  ],
  "categories": [
    "api-schemas",
    "observability",
    "agent-runtime",
    "status"
  ],
  "source_docs": [
    "bca65683c571fc59"
  ],
  "backlinks": null,
  "word_count": 525,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The status API is the real-time observability surface for a running PocketPaw instance. It answers two questions: what is each active agent session currently doing, and what is the overall health of the runtime? Three Pydantic models form this response.

## Models

### `SessionStatus`

```python
class SessionStatus(BaseModel):
    session_key: str
    session_id: str
    channel: str
    title: str | None = None
    state: str  # thinking, tool_running, streaming, waiting_for_user, error
    tool_name: str | None = None
    duration_seconds: float = 0
    token_usage: dict[str, int] | None = None
    error_message: str | None = None
```

This model tracks the execution lifecycle of a single agent invocation.

**`session_key` vs `session_id`** — two distinct identifiers exist intentionally. `session_id` is the stable, persistent identifier for the conversation thread. `session_key` is the ephemeral key for the current agent invocation within that session — it changes each time the agent is invoked and is used for plan-mode approval (see `plan_mode.py`). Having both allows the dashboard to correlate real-time status with historical session data.

**`state`** is the execution phase discriminator. The commented values define the agent's state machine:
- `thinking` — LLM is generating a response.
- `tool_running` — agent is executing a tool call; `tool_name` identifies which tool.
- `streaming` — response is being streamed to the client.
- `waiting_for_user` — agent has asked a clarifying question or is in plan mode.
- `error` — execution failed; `error_message` carries the reason.

**`tool_name`** is `None` except during `tool_running` state, giving the dashboard a specific label for what's happening (e.g. `"bash"`, `"read_file"`, `"web_search"`).

**`token_usage: dict[str, int] | None`** — flexible token reporting. The dict typically carries keys like `{"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540}`. Using a dict rather than typed fields accommodates different LLM providers that report token usage differently.

**`duration_seconds: float`** — elapsed time for the current invocation. Useful for detecting stuck sessions (a session in `thinking` state for 120 seconds is likely stalled).

### `GlobalStatus`

```python
class GlobalStatus(BaseModel):
    state: str  # idle, active, degraded
    active_sessions: int = 0
    max_concurrent: int = 5
    uptime_seconds: int = 0
```

`state` has three values: `idle` (no active sessions), `active` (normal operation), `degraded` (partial failure — some sessions may be erroring). `max_concurrent: int = 5` is the concurrency ceiling. Exposing it in the status response lets the dashboard show a progress indicator ("3 of 5 slots in use") without a separate configuration endpoint call.

### `AgentStatusResponse`

```python
class AgentStatusResponse(BaseModel):
    global_status: GlobalStatus = Field(alias="global")
    sessions: list[SessionStatus] = []

    model_config = {"populate_by_name": True}
```

The top-level envelope. `Field(alias="global")` is required because `global` is a Python reserved keyword — the JSON key is `"global"` but the Python attribute is `global_status`. `populate_by_name: True` allows the model to be instantiated using either the alias or the Python name, enabling clean internal code while maintaining the external JSON contract.

## Known Gaps

- `state` on both `SessionStatus` and `GlobalStatus` is an unconstrained string. `Literal` unions would make state machine transitions type-checkable.
- No `last_error_at` timestamp on `SessionStatus` — when a session recovers from error, there's no record of when the error occurred.
- `max_concurrent` is read-only in this schema; there's no corresponding settings field to change the concurrency limit via the API.