---
{
  "title": "Intentions API Tests: CRUD, Toggle, and On-Demand Execution",
  "summary": "This test file covers PocketPaw's `/api/v1/intentions` router, which manages proactive agent intentions — scheduled or triggered actions the agent initiates autonomously. It verifies the full CRUD lifecycle, enable/disable toggling, input validation, and on-demand execution.",
  "concepts": [
    "intentions",
    "ProactiveDaemon",
    "cron trigger",
    "CRUD",
    "toggle enabled",
    "on-demand run",
    "proactive agent",
    "context sources",
    "AsyncMock",
    "422 validation",
    "not-found handling"
  ],
  "categories": [
    "proactive agent",
    "API",
    "testing",
    "scheduling",
    "test"
  ],
  "source_docs": [
    "3ac79cfd5fd2c954"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Intentions are PocketPaw's mechanism for proactive agent behaviour. An intention pairs a prompt with a trigger (e.g. a cron schedule) and optional context sources. When triggered, the agent runs the prompt autonomously and delivers the result to the configured channel. The intentions API exposes CRUD operations over these objects, delegating to a background `ProactiveDaemon`.

## Sample Intention Fixture

A module-level `_SAMPLE_INTENTION` dict represents a realistic intention — a "Morning Standup" that fires at 8 AM on weekdays:

```python
_SAMPLE_INTENTION = {
    "id": "int-001",
    "name": "Morning Standup",
    "prompt": "What are your top 3 priorities?",
    "trigger": {"type": "cron", "schedule": "0 8 * * 1-5"},
    "context_sources": ["system_status"],
    "enabled": True,
    "created_at": "2026-02-20T08:00:00",
    "last_run": None,
    "next_run": "2026-02-21T08:00:00",
}
```

Using a realistic fixture ensures field names and types match what the frontend expects, catching serialisation mismatches early.

## List (`GET /intentions`)

Tested with both an empty result and a populated result. The response wraps the list under an `intentions` key rather than returning a bare array, which allows the envelope to carry metadata (pagination, totals) in the future without breaking existing clients.

## Create (`POST /intentions`)

Four test scenarios:

- **Success**: Returns `{"intention": {...}}` with the created object.
- **Missing name**: FastAPI's Pydantic validation returns 422 automatically — the route handler never runs.
- **Missing prompt**: Same as above.
- **Runtime failure**: If `daemon.create_intention` raises `RuntimeError`, the route returns 500 with the exception message in `detail`. This is intentional — it surfaces daemon errors to the user rather than swallowing them as an opaque failure.

## Update (`PATCH /intentions/{id}`)

- **Success**: Returns the updated intention.
- **Not found**: `daemon.update_intention` returning `None` triggers a 404.
- **No fields**: An empty `{}` body returns 400 with "no updates" in the detail. This prevents a no-op PATCH from being silently accepted as a successful update, which would be misleading to the caller.

## Delete (`DELETE /intentions/{id}`)

- **Success**: `daemon.delete_intention` returns `True`; response is `{"deleted": true}`.
- **Not found**: Returns `False`; triggers 404.

## Toggle (`POST /intentions/{id}/toggle`)

Flips the `enabled` state of an intention without requiring a full PATCH. The test confirms that toggling a previously-enabled intention to `enabled: false` is correctly reflected in the response. Not-found returns 404.

## Run (`POST /intentions/{id}/run`)

Triggers immediate execution of an intention outside its normal schedule. The daemon's `run_intention_now` is an async method, so it is patched with `AsyncMock`. The response returns `{"status": "running", "message": "..."}` where the message includes the intention name. This lets the dashboard display a human-readable confirmation. Not-found returns 404.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: what happens if `run_intention_now` raises (the error path), concurrent toggle + run operations, or listing with pagination parameters.