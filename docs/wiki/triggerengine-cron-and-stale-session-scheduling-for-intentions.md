---
{
  "title": "TriggerEngine: Cron and Stale-Session Scheduling for Intentions",
  "summary": "TriggerEngine wraps APScheduler to provide two trigger types for proactive intentions: cron-based scheduling (standard 5-field cron expressions plus named presets) and stale-session polling (fires when a WebSocket session has been idle beyond a threshold). It manages job registration, removal, and update for all enabled intentions.",
  "concepts": [
    "TriggerEngine",
    "cron scheduling",
    "APScheduler",
    "stale-session triggers",
    "cron presets",
    "job management",
    "rate limiting",
    "session polling",
    "intention scheduling",
    "stale threshold"
  ],
  "categories": [
    "Daemon",
    "Scheduling"
  ],
  "source_docs": [
    "6403f79c47a0502b"
  ],
  "backlinks": null,
  "word_count": 437,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`TriggerEngine` in `src/pocketpaw/daemon/triggers.py` is the scheduling backbone of PocketPaw's proactive system. It translates the `trigger` dict in each intention into APScheduler jobs and fires a callback when those jobs run. Two trigger types are supported: `cron` (time-based) and `stale_session` (activity-based).

## Cron Triggers

Cron triggers map directly to APScheduler's `CronTrigger`. The `parse_cron_expression()` function accepts either:

- **Standard 5-field cron** — `"0 8 * * 1-5"` (8am on weekdays)
- **Named presets** — strings from `CRON_PRESETS` like `"weekday_morning_9am"` or `"every_15_minutes"`

Presets exist because most users don't know cron syntax. Offering `"every_morning_8am"` as a human-readable alias reduces friction and prevents malformed expressions.

When `_add_cron_trigger()` adds a job, it uses `intention_id` as the APScheduler job ID. This ensures one-to-one mapping: adding the same intention twice doesn't create duplicate jobs (APScheduler replaces the existing job with matching ID).

## Stale-Session Triggers

The stale-session trigger is an interval job that polls session activity. Every `_DEFAULT_STALE_CHECK_MINUTES` (60 minutes), `_fire_stale_trigger()` calls `_find_stale_sessions()` which reads `last_activity` timestamps from the memory manager. Sessions idle beyond `_DEFAULT_STALE_THRESHOLD_HOURS` (12 hours) are candidates.

### Rate-Limiting Nudges

The `_nudged_` dict (keyed by `session_key`) records when each stale session last received a nudge. A session won't receive two nudges within `stale_threshold_hours * 2` hours. Without this guard, a session idle for days would fire a nudge every 60 minutes indefinitely — the user would be spammed.

### Session Metadata Injection

When a stale-session trigger fires, it passes `session_meta` to the executor: `{session_key, title, idle_hours, preview}`. This gives the intention prompt access to `{{session.title}}` and `{{session.idle_hours}}` so the notification can reference the specific idle session.

## Job Management

- **`add_intention()`** — routes to `_add_cron_trigger` or `_add_stale_trigger` based on `trigger.type`.
- **`remove_intention()`** — removes the APScheduler job by intention ID. Safe to call if the job doesn't exist.
- **`update_intention()`** — removes then re-adds. APScheduler doesn't support in-place trigger reconfiguration.
- **`remove_all_jobs()`** — used during `stop()` and `reload_intentions()` to clear all scheduled jobs.
- **`run_now()`** — creates an `asyncio.create_task()` that calls `_fire_trigger()` immediately, bypassing the schedule. Used by the "Run Now" dashboard action.

## Scheduler Ownership

`TriggerEngine` accepts an optional external `AsyncIOScheduler` via the constructor. If not provided, it creates and owns its own. The shared-scheduler path is used when the dashboard's APScheduler instance (which also runs health heartbeats) should be reused to avoid spinning up multiple event loops.

## Known Gaps

- **File-watch triggers** are documented as future work but not yet implemented. The `add_intention()` method logs a warning and returns `False` for unknown trigger types.
- Stale-session detection depends on `MemoryManager` returning `last_activity` — if memory is disabled or the session format changes, `_find_stale_sessions()` may return empty results silently.