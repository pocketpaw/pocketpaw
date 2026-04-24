---
{
  "title": "Proactive Daemon Tests: Cron Scheduling, Intention CRUD, Context Hub, and Stale Session Triggers",
  "summary": "This test module covers PocketPaw's proactive daemon — the subsystem that fires agent prompts on a schedule rather than waiting for user messages. It validates cron expression parsing (including named presets), the `IntentionStore` CRUD lifecycle, `ContextHub` template variable injection, the `TriggerEngine` scheduling layer, and the stale-session trigger that nudges idle users.",
  "concepts": [
    "ProactiveDaemon",
    "IntentionStore",
    "TriggerEngine",
    "ContextHub",
    "cron parsing",
    "CRON_PRESETS",
    "stale session trigger",
    "rate limiting",
    "template variables",
    "intention lifecycle",
    "schedule",
    "idle nudge"
  ],
  "categories": [
    "agent runtime",
    "testing",
    "scheduling",
    "proactive features",
    "test"
  ],
  "source_docs": [
    "b1c2cb411c251470"
  ],
  "backlinks": null,
  "word_count": 610,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Most AI agents are reactive — they respond to messages. PocketPaw's proactive daemon inverts this: it fires agent prompts on a schedule, enabling use cases like daily briefings, automated check-ins, and idle-session nudges. This test file specifies the behavior of every layer in that subsystem.

## Cron Expression Parsing (`TestCronParsing`)

`parse_cron_expression` accepts both standard 5-field cron strings (`"0 8 * * 1-5"`) and named presets (`"weekday_morning_8am"`). The test for standard parsing asserts each of the five fields is correctly extracted as a string. The preset test confirms named aliases expand to the same field values.

`test_available_presets` checks that commonly needed schedules (`every_minute`, `every_5_minutes`, `every_hour`, `every_morning_8am`, `weekday_morning_9am`) exist in `CRON_PRESETS`. This acts as a registry contract — if a preset is renamed, this test catches it before operators' configurations break.

Invalid inputs — non-cron strings and cron expressions with fewer than 5 fields — must raise `ValueError`, preventing silent misconfiguration where an intention is created but never fires.

## IntentionStore CRUD (`TestIntentionStore`)

An `intention` is a named agent task with a prompt template, a trigger specification, and an enabled flag. The store is backed by a JSON file whose path is monkeypatched to a temp directory.

`test_create_intention` verifies that a newly created intention has a non-empty `id`, the correct `name` and `prompt`, `enabled=True` by default, and a non-empty `created_at` timestamp. These assertions prevent regressions where required fields are omitted from the persisted dict.

The CRUD suite covers `get_by_id`, `update` (partial field updates), `delete` (removes from the store, subsequent `get_by_id` returns `None`), `toggle` (flips the `enabled` flag), and `get_enabled_intentions` (filters out disabled intentions for the TriggerEngine).

## ContextHub (`TestContextHub`)

Intention prompts use template variables like `{{datetime.time}}` and `{{system.status}}`. `ContextHub` resolves these at execution time by gathering context from registered sources.

`test_gather_system_status` asserts the status dict contains expected keys (uptime, active sessions, etc.). `test_gather_datetime` confirms datetime fields are present and non-empty. `test_apply_template` confirms `{{datetime.time}}` is replaced with an actual time string in the rendered prompt. `test_format_context_string` validates the full serialized context string format.

## TriggerEngine (`TestTriggerEngine`)

The `TriggerEngine` manages the lifecycle of scheduled jobs. `test_add_cron_trigger` adds an intention with a cron trigger and asserts a job is registered. `test_remove_trigger` verifies that removing a job causes it to no longer appear in the active job list. `test_disabled_intention_not_scheduled` confirms that disabled intentions are skipped during scheduling — preventing unwanted agent messages after an operator disables an intention.

## ProactiveDaemon Integration (`TestProactiveDaemon`)

The daemon fixture starts the daemon against a temp directory and stops it after each test. `test_daemon_lifecycle` asserts the daemon reaches a running state. The create/toggle/delete tests drive the daemon's public API rather than the underlying `IntentionStore` directly, verifying the integration between the daemon facade and its storage layer.

## Stale Session Trigger (`TestStaleSessionTrigger`)

This trigger type fires when a user session has been idle beyond a configurable threshold — useful for check-in nudges. Key behavioral tests:

`test_stale_trigger_calls_callback_for_stale_sessions` patches `list_sessions_with_metadata` to return a session whose `last_active` is older than the threshold and asserts the daemon callback is invoked.

`test_stale_trigger_rate_limits_nudges` confirms that a session is not nudged more than once within the rate-limit window, preventing the agent from spamming idle users with repeated messages.

`test_executor_injects_session_variables` verifies that `{{session.user_id}}` and `{{session.idle_minutes}}` are available in the rendered prompt, allowing the intention's message to reference the specific idle user.

`test_eviction_removes_expired_entries` confirms that the internal rate-limit tracking dict evicts stale entries over time, preventing unbounded memory growth in long-running deployments.

## Known Gaps

No TODO or FIXME markers are present. The `TestTriggerEngine` tests use a `noop` async function as the callback, so they only verify that the job is registered, not that it fires correctly on schedule. Actual firing is tested indirectly through the stale-session trigger tests.