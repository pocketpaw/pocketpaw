---
{
  "title": "Intention Scheduling Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's intention system — scheduled, trigger-driven agent tasks that run prompts automatically. The schema covers the full CRUD lifecycle plus manual trigger acknowledgement, with field-level validation to prevent empty or oversized intentions.",
  "concepts": [
    "IntentionTrigger",
    "IntentionInfo",
    "CreateIntentionRequest",
    "UpdateIntentionRequest",
    "RunIntentionResponse",
    "cron scheduling",
    "proactive agent tasks",
    "context sources",
    "partial update",
    "Pydantic validation"
  ],
  "categories": [
    "api-schemas",
    "intentions",
    "scheduling",
    "automation"
  ],
  "source_docs": [
    "ff691148e03aa707"
  ],
  "backlinks": null,
  "word_count": 542,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Intentions are proactive agent tasks: a user defines a prompt and a trigger (typically a cron schedule), and PocketPaw fires the agent automatically when the trigger fires. This is the "set and forget" automation layer — the agent can draft daily summaries, check on background jobs, send reminders, or poll external services without user intervention.

This module defines the complete API schema surface for managing intentions.

## Core Concepts

### `IntentionTrigger`

```python
class IntentionTrigger(BaseModel):
    type: str = "cron"
    schedule: str = ""
```

`type` defaults to `"cron"` as the primary trigger mechanism. The `schedule` field holds a cron expression (e.g. `"0 9 * * 1-5"` for weekday mornings). Keeping `type` as a plain string leaves room for future trigger kinds — webhook, event-driven, or interval-based — without a breaking schema change.

### `IntentionInfo`

The canonical read representation of an intention:

```python
class IntentionInfo(BaseModel):
    id: str
    name: str
    prompt: str
    trigger: IntentionTrigger | dict = {}
    context_sources: list[str] = []
    enabled: bool = True
    created_at: str = ""
    last_run: str | None = None
    next_run: str | None = None
```

`trigger` accepts both a typed `IntentionTrigger` and a raw `dict` — a pragmatic union that avoids validation failures when stored intentions use trigger shapes not yet modelled. `last_run` and `next_run` are nullable strings; `None` indicates the intention hasn't run yet or the scheduler hasn't computed the next execution time.

`context_sources` is a list of identifiers for data sources (e.g. calendar feeds, file paths, web URLs) that should be injected into the agent's context when the intention fires. This lets the agent act on current information rather than working from stale context.

## CRUD Operations

### `CreateIntentionRequest`

```python
class CreateIntentionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=10000)
    trigger: dict = {}
    context_sources: list[str] = []
    enabled: bool = True
```

Field constraints are deliberate: `name` is capped at 200 characters to keep UI listings readable; `prompt` at 10,000 characters to prevent runaway token costs on every trigger fire. The `min_length=1` guards prevent creating intentions with empty names or blank prompts, which would produce confusing agent behaviour.

### `UpdateIntentionRequest`

```python
class UpdateIntentionRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    trigger: dict | None = None
    context_sources: list[str] | None = None
    enabled: bool | None = None
```

All fields are optional — a pure partial-update model. The backend updates only the supplied non-`None` fields. This is particularly important for toggling `enabled` without touching the prompt.

## Response Models

- **`IntentionResponse`** — wraps a single `IntentionInfo` for create/read/update responses.
- **`DeleteIntentionResponse`** — returns the deleted `id` and `deleted: bool = True` to confirm the operation.
- **`RunIntentionResponse`** — acknowledges a manual trigger with `status: str = "running"`. The `"running"` default signals that execution is asynchronous; the actual result arrives later via SSE.

## Known Gaps

- `trigger: dict` on `CreateIntentionRequest` is unvalidated. Malformed cron expressions are accepted at the schema level and only fail at scheduler runtime.
- No deduplication guard — two identical intentions can be created. A unique constraint on `name` or `(name, trigger)` would prevent accidental duplicates.
- `UpdateIntentionRequest` has no minimum-length validators on `name` and `prompt` (unlike `CreateIntentionRequest`), so an update could set `name` to an empty string.