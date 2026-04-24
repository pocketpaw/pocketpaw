---
{
  "title": "Intentions Router — Proactive Agent Scheduling CRUD and Manual Trigger",
  "summary": "The intentions router manages PocketPaw's proactive behavior system — scheduled or event-triggered agent prompts that run autonomously without user initiation. It exposes full CRUD operations plus a toggle (enable/disable without deleting) and a run-now action that bypasses the scheduler for immediate execution.",
  "concepts": [
    "intentions",
    "proactive agent",
    "scheduler",
    "cron trigger",
    "IntentionInfo",
    "toggle enable",
    "run-now",
    "proactive daemon",
    "context_sources",
    "next_run",
    "CRUD"
  ],
  "categories": [
    "API",
    "Automation",
    "Scheduling"
  ],
  "source_docs": [
    "8bd2f5e21b2385d9"
  ],
  "backlinks": null,
  "word_count": 371,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Intentions are the mechanism by which PocketPaw agents act proactively. Rather than waiting for a user message, an intention defines a prompt and a trigger — a cron schedule, a time-of-day window, or an event condition — and the proactive daemon fires the prompt automatically. The intentions router is the REST control plane for this system.

## The `_to_info` Conversion Function

The proactive daemon stores intentions internally as raw dicts. The router converts these to typed `IntentionInfo` Pydantic models via `_to_info()`:

```python
def _to_info(d: dict) -> IntentionInfo:
    return IntentionInfo(
        id=d.get("id", ""),
        name=d.get("name", ""),
        prompt=d.get("prompt", ""),
        trigger=d.get("trigger", {}),
        context_sources=d.get("context_sources", []),
        enabled=d.get("enabled", True),
        created_at=d.get("created_at", ""),
        last_run=d.get("last_run"),
        next_run=d.get("next_run"),
    )
```

The explicit `.get()` calls with defaults protect against partially-formed intention dicts that might exist from earlier schema versions. A missing field won't raise a `KeyError` and break the list endpoint.

## CRUD Operations

- **List** (`GET /intentions`): Returns all intentions via the proactive daemon's `get_intentions()`.
- **Create** (`POST /intentions`): Validates the new intention through the daemon, which handles trigger parsing and assigns a unique ID.
- **Update** (`PATCH /intentions/{id}`): Partial update — only supplied fields are changed. The daemon handles recalculating `next_run` after a trigger change.
- **Delete** (`DELETE /intentions/{id}`): Removes the intention and cancels any pending scheduled execution.

## Toggle vs. Delete

`POST /intentions/{id}/toggle` flips the `enabled` flag without deleting the intention. This is important for operators who want to temporarily pause a scheduled prompt (e.g., a morning briefing during a vacation) without losing the configuration. The daemon respects the `enabled` flag when evaluating whether to fire.

## Manual Run

`POST /intentions/{id}/run` bypasses the scheduler and fires the intention immediately. This is valuable for testing a new intention before committing to a schedule, or for one-off manual triggers of prompts that normally run on a long cadence (e.g., a weekly report).

The async delegation to the daemon via `asyncio.create_task` (or equivalent) means the endpoint returns immediately with a `RunIntentionResponse` rather than waiting for the full agent turn to complete — preventing HTTP timeouts on long-running prompts.

## Known Gaps

The intentions router has no explicit scope guard — all operations are accessible to any authenticated caller. For multi-user deployments, this could allow one user to modify or delete another user's intentions.