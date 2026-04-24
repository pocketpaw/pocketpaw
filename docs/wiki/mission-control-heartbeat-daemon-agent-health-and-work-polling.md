---
{
  "title": "Mission Control Heartbeat Daemon: Agent Health and Work Polling",
  "summary": "Tests for HeartbeatDaemon, the background process that periodically wakes registered agents, checks for pending tasks and notifications, and records liveness signals. Covers daemon lifecycle, configurable intervals, work discovery, error resilience, and manual triggers.",
  "concepts": [
    "HeartbeatDaemon",
    "agent heartbeat",
    "work polling",
    "background daemon",
    "interval configuration",
    "cycle",
    "error resilience",
    "manual trigger",
    "notifications",
    "liveness",
    "multi-agent"
  ],
  "categories": [
    "multi-agent",
    "orchestration",
    "testing",
    "background tasks",
    "test"
  ],
  "source_docs": [
    "f384f9b0a52ca2fb"
  ],
  "backlinks": null,
  "word_count": 535,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

In a multi-agent system, agents must signal that they are alive and check for new work without requiring a persistent connection to Mission Control. `HeartbeatDaemon` fills this role: it runs in the background, waking each agent on a configurable interval to record a heartbeat, check for queued tasks, and deliver pending notifications. This test file validates the full daemon lifecycle and its resilience to failures.

## Daemon Lifecycle (`TestHeartbeatDaemon`)

- `test_init_default_interval` / `test_init_custom_interval`: The daemon accepts a configurable `interval_minutes`. Tests fixture uses 1 minute to avoid slow tests.
- `test_start_stop`: Starting the daemon kicks off the background coroutine; stopping it sets a flag that terminates the cycle. The test confirms the daemon actually stops rather than running indefinitely after `stop()` is called.
- `test_start_twice_warns`: Calling `start()` on an already-running daemon should log a warning, not start a second loop. A double-start would double the wakeup frequency, potentially overwhelming agents with redundant checks.
- `test_set_interval`: The interval can be changed while the daemon is running and takes effect on the next cycle.

## Agent Waking (`TestWakeAgent`)

`wake_agent` is the per-agent operation called each cycle:

- `test_wake_agent_records_heartbeat`: Each wakeup records a heartbeat timestamp in Mission Control, confirming the agent is alive. Without this, the dashboard would show agents as "unknown" after the first cycle.
- `test_wake_agent_with_callback`: Callers can inject a callback that runs after the heartbeat is recorded — used by integrations that need to perform custom work on each tick.
- `test_check_for_work_no_work`: When an agent has no pending tasks or notifications, `check_for_work` returns quickly without error.
- `test_check_for_work_with_tasks`: When pending tasks exist, they are returned so the caller can schedule execution.
- `test_check_for_work_with_notifications`: Pending notifications are returned alongside tasks, enabling agents to process them in priority order.

## Full Heartbeat Cycle (`TestHeartbeatCycle`)

- `test_cycle_wakes_all_agents`: One cycle iteration calls `wake_agent` for every registered active agent. If any agent is skipped, it accumulates stale timestamps and may be incorrectly flagged as dead.
- `test_cycle_stops_when_not_running`: The cycle loop checks the `_running` flag at the top of each iteration and exits immediately when it becomes `False`. This prevents a zombie loop that continues running after `stop()`.
- `test_cycle_handles_errors`: If `wake_agent` raises for one agent, the error is logged and the cycle continues to the next agent. Without this guard, a single misbehaving agent would halt heartbeats for all others.

The `stop_after_one` helper in the cycle tests patches `wake_agent` to call `daemon.stop()` after processing the first agent, allowing the test to observe one full iteration without waiting for a real timer.

## Manual Triggers (`TestManualTrigger`)

- `test_trigger_heartbeat`: A manual trigger runs a single heartbeat cycle immediately without waiting for the timer. This is used by tests and by administrative tooling that needs to force-sync agent state.

## Fixture Design

The `patched_daemon` fixture monkeypatches the manager reference inside the daemon so that test-controlled store and manager instances are used. This avoids the daemon accidentally reaching the real singleton, which would couple test outcomes to prior test state.

## Known Gaps

No TODOs in the file. The tests do not cover the behavior when the daemon's timer fires while a previous cycle is still executing — in a slow system, cycles could overlap, potentially recording duplicate heartbeats or running `check_for_work` concurrently.
