---
{
  "title": "Reminder Management Schemas",
  "summary": "Defines Pydantic models for PocketPaw's reminder system, which accepts natural-language reminder requests, parses them into scheduled entries, and delivers notifications when the trigger time arrives. The schema design keeps the creation API intentionally simple to support conversational input.",
  "concepts": [
    "ReminderInfo",
    "AddReminderRequest",
    "ReminderListResponse",
    "AddReminderResponse",
    "natural language parsing",
    "scheduled notifications",
    "reminder system",
    "Pydantic",
    "time_remaining",
    "NLP"
  ],
  "categories": [
    "api-schemas",
    "reminders",
    "scheduling",
    "natural-language"
  ],
  "source_docs": [
    "a8f33f6624120662"
  ],
  "backlinks": null,
  "word_count": 521,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Reminders in PocketPaw allow users to ask the agent to notify them at a future time, using natural language rather than structured datetime input. The agent parses the natural language, schedules the trigger, and fires a notification at the right time. This module defines the five models that cover the reminder lifecycle: create, list, read, and delete.

## Models

### `ReminderInfo`

The canonical read representation of a reminder:

```python
class ReminderInfo(BaseModel):
    id: str
    text: str
    trigger_at: str
    created_at: str
    time_remaining: str = ""
```

`trigger_at` and `created_at` are strings rather than `datetime` objects, keeping timezone handling in the application layer. `time_remaining` is a pre-formatted human-readable string (e.g. `"in 2 hours 15 minutes"`) rather than a raw duration. This is a UX choice: the dashboard can display countdown text directly without computing it client-side, and the format can be localised server-side.

### `ReminderListResponse`

```python
class ReminderListResponse(BaseModel):
    reminders: list[ReminderInfo]
```

A simple envelope. No pagination metadata is included — this implies the backend returns all active (non-expired) reminders. The assumption is that users have a small number of reminders at any time, making pagination unnecessary for the expected use case.

### `AddReminderRequest`

```python
class AddReminderRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
```

This is the most opinionated schema in the file. Rather than accepting a structured `{"text": "...", "datetime": "..."}` payload, the API takes a single natural-language `message` string: `"Remind me to check the deploy logs in 30 minutes"`. The agent's NLP layer parses the time expression and intention from the message.

`min_length=1` prevents empty reminders. `max_length=5000` caps the input to prevent abuse and to bound the LLM context consumed by NLP parsing. This is a practical limit that should cover any reasonable reminder message.

### `AddReminderResponse`

```python
class AddReminderResponse(BaseModel):
    reminder: ReminderInfo
```

Returns the newly created `ReminderInfo` immediately, including the parsed `trigger_at` and computed `time_remaining`. The caller can confirm the agent correctly interpreted the natural-language time expression (e.g. `"in 30 minutes"` was parsed as the right timestamp) before leaving the page.

### `DeleteReminderResponse`

```python
class DeleteReminderResponse(BaseModel):
    id: str
    deleted: bool = True
```

Echoes the deleted reminder's `id` and confirms deletion. `deleted: bool = True` is a semantic signal — in an idempotent delete design, deleting an already-absent reminder could still return `deleted: True` (the desired state was achieved) or `deleted: False` (the reminder was already gone). The default of `True` assumes the happy path; the backend handler determines the actual value.

## Defensive Patterns

- `max_length=5000` on `AddReminderRequest.message` bounds LLM parsing cost and prevents oversized payloads.
- `AddReminderResponse` returns the parsed reminder immediately, enabling the user to catch NLP misinterpretations before they become missed deadlines.
- `time_remaining` defaults to `""` rather than `None`, avoiding null-check logic in the dashboard.

## Known Gaps

- No timezone field on `AddReminderRequest`. The NLP parser must infer timezone from context (user profile, system clock), which can be wrong for users in ambiguous timezone situations.
- No recurrence support — reminders are single-fire only. A `recurrence` field would enable `"every Monday morning"` style reminders.
- `ReminderListResponse` has no pagination. Large reminder lists (if reminders are ever persisted long-term) would return everything in one response.