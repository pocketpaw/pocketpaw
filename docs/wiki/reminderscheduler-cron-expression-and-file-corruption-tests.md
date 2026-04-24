---
{
  "title": "ReminderScheduler Cron Expression and File Corruption Tests",
  "summary": "This test file validates `ReminderScheduler`'s support for recurring cron-based reminders and its resilience to corrupted or malformed reminder files. Tests cover cron expression validation, preset schedules, persistent-vs-oneshot behavior, file quarantine on corruption, and partial corruption recovery.",
  "concepts": [
    "ReminderScheduler",
    "cron expression",
    "recurring reminder",
    "one-shot reminder",
    "file quarantine",
    "JSON corruption",
    "scheduler restart",
    "load_reminders",
    "save_reminders",
    "RemindersCorruptError",
    "schedule preset"
  ],
  "categories": [
    "testing",
    "scheduler",
    "resilience",
    "persistence",
    "test"
  ],
  "source_docs": [
    "756233ad55231b42"
  ],
  "backlinks": null,
  "word_count": 494,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_scheduler_cron.py` (created 2026-02-06) covers two related concerns in `pocketpaw.scheduler`: the `add_recurring` / `delete_recurring` cron API (Feature 5), and defensive file loading behavior against corrupt JSON.

## Cron Expression Support

`TestCronExpressionSupport` validates `ReminderScheduler.add_recurring(text, schedule)`:

- **Valid cron string** (`"0 9 * * *"`) — returns a reminder dict with `type == "recurring"`, the correct `schedule` field, and an `id`. `save_reminders` is called, confirming persistence.
- **Preset string** (`"every_morning_8am"`) — preset named schedules are accepted in addition to raw cron syntax, allowing agents to offer human-friendly schedule names.
- **Invalid cron** (`"not a cron"`) — returns `None` rather than raising. This is a deliberate design choice: agents call this method and need to detect failure without a try/except.
- **Multiple additions** — both reminders appear in `scheduler.reminders` in insertion order.
- **Delete** — `delete_recurring(id)` returns `True` and removes the entry.

### Recurring vs. One-Shot Lifecycle

`test_recurring_reminder_not_removed_on_trigger` verifies that when a recurring reminder fires, it stays in the list (so it fires again next time). `test_oneshot_reminder_removed_on_trigger` verifies the opposite for regular reminders. This distinction is the core behavioral contract: without it, daily standup reminders would self-delete after the first fire.

### Scheduler Restart Behavior

`test_start_reschedules_recurring` checks that `ReminderScheduler.start()` re-arms recurring reminders from disk after a restart, so reminders survive process restarts. `test_start_skips_past_oneshot` checks that a one-shot reminder with a past `fire_at` is skipped rather than immediately triggered on startup — preventing a flood of stale notifications after a long downtime.

## File Corruption Handling

`TestReminderFileCorruption` covers the full range of malformed reminder files:

- **Corrupt JSON** — the file exists but contains invalid JSON. The scheduler quarantines the file (moves it to a `.quarantine` path) and returns an empty list.
- **Non-dict root** — valid JSON but the top-level value is a string or list instead of `{"reminders": [...]}`. Quarantined.
- **Non-UTF-8 bytes** — file contains binary garbage. Quarantined.
- **Invalid reminder schema** — the list exists but entries are missing required fields. The whole file is quarantined.
- **Partial corruption** — some entries are valid and some are not. Valid entries are preserved; invalid ones are skipped. This is important for resilience — one bad reminder should not wipe out all others.
- **OSError** — file cannot be read (permissions, etc.). Returns empty list without crashing.
- **Quarantine failure** — if the quarantine move itself fails, the error is re-raised rather than silently swallowed. This prevents an infinite loop where a corrupt file is repeatedly loaded and fails to quarantine.
- **Missing `schedule` on recurring** — a recurring entry without a `schedule` field is skipped during load.
- **Unique quarantine filenames** — two corrupt loads produce distinct quarantine filenames, preventing overwrites.

```python
def test_load_reminders_quarantines_corrupt_json(tmp_path):
    # Writes invalid JSON, then asserts a .quarantine file was created
```

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover clock-based cron calculation (i.e., whether `"0 9 * * *"` actually fires at 9am), which is likely tested separately in scheduler timing tests.
