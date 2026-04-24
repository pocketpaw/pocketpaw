---
{
  "title": "Automations Bridge — Syncing EE Rules to Core Daemon Intentions",
  "summary": "bridge.py translates enterprise automation rules into core daemon Intention specs, and removes them when rules are deleted. It maps schedule presets and rule types (schedule, threshold, data_change) to cron, interval, and event trigger types respectively, ensuring automation rules run through the same scheduling infrastructure as built-in daemon behaviors.",
  "concepts": [
    "rule_to_intention_spec",
    "unsync_rule_from_daemon",
    "automation bridge",
    "daemon Intention",
    "schedule rules",
    "threshold rules",
    "data_change rules",
    "SCHEDULE_TO_CRON",
    "cron expressions",
    "EE integration",
    "trigger types"
  ],
  "categories": [
    "enterprise-edition",
    "automations",
    "scheduling",
    "daemon-integration"
  ],
  "source_docs": [
    "b4ab69a5fd471d21"
  ],
  "backlinks": null,
  "word_count": 495,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Enterprise automation rules are authored by users in EE-specific terms (`schedule: "Every Monday 9am"`, `type: threshold`). The core daemon understands `Intention` objects with trigger types. `bridge.py` is the translation layer that converts between these two vocabularies, keeping EE rules integrated with core scheduling rather than running a parallel scheduler.

## `rule_to_intention_spec`

This function converts a `Rule` object into a dict that the core daemon can use to create an Intention:

### Schedule Rules

Cron expression or preset string → Intention with `cron` trigger. The module defines `SCHEDULE_TO_CRON`:

```python
SCHEDULE_TO_CRON = {
    "Every Monday 9am": "0 9 * * 1",
    "Daily at 8am": "0 8 * * *",
    "Every hour": "0 * * * *",
    "Every 15 minutes": "*/15 * * * *",
    # ...
}
```

If the schedule value is already a valid cron expression (detected by presence of spaces and `*`), it passes through directly. If it's a preset, it's looked up in `SCHEDULE_TO_CRON`. Unknown presets fall back to a daily schedule with a warning log — this prevents a bad preset from creating an intention with no trigger.

### Threshold Rules

Threshold rules fire when a metric crosses a threshold (e.g., inventory below 10). These map to Intentions with `interval` triggers — the evaluator runs periodically and checks the condition. The intention spec includes a prompt describing the condition so the daemon knows what to evaluate.

### Data Change Rules

Data change rules fire when a watched field changes value. These map to Intentions with `event` triggers (poll-based, since the system doesn't have live change detection for arbitrary data sources). The intention spec includes the object type, property, and operator.

## `unsync_rule_from_daemon`

When a rule is deleted (via the automations router), `unsync_rule_from_daemon` removes the linked daemon Intention. The rule carries a `linked_intention_id` field set when the bridge first creates the intention.

The function returns `bool` — `True` if the intention was found and deleted, `False` if it was already gone. The `False` case is not an error — it means the intention was deleted independently (e.g., a daemon reset), and the rule deletion should still succeed.

## Why Bridge, Not Direct Daemon Calls

The bridge pattern separates concern: the automations module doesn't need to know how the daemon manages intentions internally. If the daemon's Intention format changes, only the bridge needs updating — the rest of the automations stack is unaffected. It also makes testing easier: bridge tests can mock the daemon call and verify the spec shape independently.

## Known Gaps

- `SCHEDULE_TO_CRON` is a hardcoded dict. There is no UI for users to define custom presets — they must use raw cron syntax.
- The fallback for unknown schedule presets (daily at 8am) could silently create unintended schedules. A strict mode that rejects unknown presets would be safer.
- `unsync_rule_from_daemon` does not handle the case where `linked_intention_id` is `None` (rule was never synced) — it would attempt to delete intention `None`, likely a no-op but not explicitly guarded.
