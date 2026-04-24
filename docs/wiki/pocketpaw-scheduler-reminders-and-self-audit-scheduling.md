---
{
  "title": "PocketPaw Scheduler: Reminders and Self-Audit Scheduling",
  "summary": "ReminderScheduler wraps APScheduler to provide one-shot and recurring reminders with natural language time parsing, per-entry schema validation, and a corrupt-file quarantine mechanism. It also manages the daily self-audit job as a built-in named APScheduler task.",
  "concepts": [
    "ReminderScheduler",
    "APScheduler",
    "RemindersCorruptError",
    "natural language time",
    "parse_natural_time",
    "recurring reminder",
    "self-audit",
    "UTC normalization",
    "schema validation",
    "quarantine",
    "CronTrigger"
  ],
  "categories": [
    "scheduling",
    "reminders",
    "fault-tolerance"
  ],
  "source_docs": [
    "488e8d06a98e2234"
  ],
  "backlinks": null,
  "word_count": 440,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scheduler.py` is PocketPaw's proactive reminder engine. It lets agents and users schedule reminders in natural language ("remind me in 5 minutes to call mom") and recurring tasks ("every morning at 8am"). It also manages the daily self-audit daemon as a built-in scheduled job.

## Corruption Recovery

The most notable defensive pattern is the corrupt-file handling pipeline:

```python
class RemindersCorruptError(RuntimeError):
    """Raised when the reminders file exists but cannot be safely parsed."""
```

When `load_reminders()` detects a JSON decode error, invalid root type, or invalid list, it calls `_signal_corrupt_reminders()` which:
1. Renames the corrupt file to `reminders.json.corrupt-{timestamp}-{uuid}` (preserving it for recovery)
2. Raises `RemindersCorruptError`

The scheduler's `start()` catches `RemindersCorruptError` and continues with an empty reminders list without writing a new empty file — avoiding accidental overwrite of the quarantine backup.

Without this recovery path, a single JSON corruption would prevent PocketPaw from starting at all.

## Per-Entry Schema Validation

Even when the JSON parses successfully, each reminder entry is validated against a schema checking for valid `id`, `text`, `trigger_at` (ISO 8601), and `schedule` (for recurring entries). Malformed entries are skipped with a warning rather than causing a total load failure.

## Natural Language Parsing

`parse_natural_time()` handles four patterns:
1. Relative: "in 5 minutes", "3 hours" (with or without "in")
2. Clock time: "at 14:30", "at 2:30pm"
3. Tomorrow reference: "tomorrow at 9am"
4. Arbitrary: delegated to `dateutil.parser.parse(fuzzy=True)` as a catch-all

The "at HH:MM" pattern automatically schedules for tomorrow if the time has already passed today — preventing the common gotcha of scheduling a reminder in the past.

## Recurring Reminders

`add_recurring()` accepts either a cron expression (`"0 8 * * *"`) or a preset name resolved by `parse_cron_expression()`. Validation runs before storage to prevent invalid schedules from being persisted.

## Self-Audit Integration

`_schedule_self_audit()` is called during `start()` if `settings.self_audit_enabled` is `True`. It schedules a daily `run_self_audit()` call as a named APScheduler job (`__self_audit__`). Failure to schedule the audit is logged as a warning, not an error — the scheduler continues even if audit setup fails.

## UTC Normalization

`_ensure_utc()` handles legacy timestamps stored without timezone info:

```python
def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt
```

Without this, comparing a naive and aware datetime raises `TypeError`.

## Known Gaps

- **Race condition on rapid add+trigger**: If a reminder's trigger time is in the past by the time `_add_job()` runs, APScheduler fires it immediately. For very short delays, this can cause double-firing.
- **`format_time_remaining()` does not handle recurring reminders correctly**: It reads `trigger_at` which is the creation time for recurring reminders, not the next scheduled occurrence. The formatted string will always show "past" for recurring reminders.