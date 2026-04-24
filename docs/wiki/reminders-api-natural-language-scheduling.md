---
{
  "title": "Reminders API — Natural Language Scheduling",
  "summary": "The reminders router provides list, add, and delete operations for time-based reminders. Reminders are created from natural language strings and processed by the scheduler, which handles parsing relative time expressions like \"in 5 minutes\" or \"tomorrow at 9am\".",
  "concepts": [
    "reminders",
    "natural language scheduling",
    "Scheduler",
    "time expressions",
    "lazy import",
    "AddReminderRequest",
    "ReminderInfo",
    "recurring reminders"
  ],
  "categories": [
    "scheduling",
    "API"
  ],
  "source_docs": [
    "9fb100168c3fae77"
  ],
  "backlinks": null,
  "word_count": 370,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw companions can set reminders on behalf of users. The `reminders.py` router is the REST interface that the dashboard (and agent tools) use to manage scheduled alerts. The design keeps the API layer thin — all scheduling intelligence lives in the `Scheduler` singleton.

## `GET /reminders`

Returns all active reminders via `get_scheduler().get_reminders()`. Each reminder is mapped to a `ReminderInfo` schema with `id`, `text`, `trigger_at`, and `repeat` fields. The mapping normalises raw scheduler dicts to typed Pydantic objects, ensuring the API response shape is stable regardless of internal scheduler changes.

## `POST /reminders`

Accepts an `AddReminderRequest` with a `text` field containing a natural language time expression. Examples the scheduler handles:

- `"in 5 minutes to call mom"`
- `"tomorrow at 9am — team standup"`
- `"every day at 7:30am for morning brief"`

The endpoint delegates parsing entirely to `get_scheduler().add_reminder(text)`. On success, the scheduler returns metadata (the parsed trigger time, the generated ID, and whether it's a repeating reminder) which is returned in `AddReminderResponse`.

If the text cannot be parsed into a valid time expression, the scheduler raises an exception which propagates as a 500. There is no dedicated 422 path for unparseable input — the parser is expected to be permissive.

## `DELETE /reminders/{reminder_id}`

Deletes a reminder by ID. The 404 guard uses `result.get("deleted")` from the scheduler's response rather than checking by exception. This keeps the scheduler interface simple and lets the router layer own the HTTP semantics.

## Dependency Injection Pattern

All three handlers use lazy imports (`from pocketpaw.scheduler import get_scheduler`) rather than module-level singletons. This is consistent with PocketPaw's router conventions: the scheduler is only instantiated when the first reminder request arrives, keeping startup cost low and making the module easier to test with a mock scheduler.

## Known Gaps

- There is no `PUT /reminders/{id}` endpoint for updating a reminder's time or text without deleting and re-creating it.
- The API has no support for recurring reminders via the REST layer — repeating reminders must be expressed entirely through the natural language text string.
- The 500 response on parse failure is not ideal UX. A dedicated validation pass that returns 422 with a clear "could not parse time expression" message would make this more self-explaining.
