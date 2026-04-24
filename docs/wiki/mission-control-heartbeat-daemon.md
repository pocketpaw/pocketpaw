---
{
  "title": "Mission Control Heartbeat Daemon",
  "summary": "The HeartbeatDaemon is a background APScheduler-driven daemon that periodically wakes every registered agent to check for pending work, update status, and fire optional event callbacks. It uses staggered agent wakeups and a singleton pattern to ensure only one daemon runs per process.",
  "concepts": [
    "HeartbeatDaemon",
    "APScheduler",
    "AsyncIOScheduler",
    "IntervalTrigger",
    "agent wakeup",
    "staggered scheduling",
    "singleton",
    "AgentStatus",
    "MissionControlManager",
    "proactive daemon"
  ],
  "categories": [
    "mission-control",
    "scheduling",
    "agent-orchestration"
  ],
  "source_docs": [
    "56946739eb7ea018"
  ],
  "backlinks": null,
  "word_count": 505,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`HeartbeatDaemon` is the proactive heartbeat engine for PocketPaw's Mission Control. Without a heartbeat mechanism, agents would only react to direct invocations — meaning @mentions, task assignments, and work queue entries would sit unacknowledged until a human explicitly triggered a poll. The heartbeat daemon solves this by introducing a recurring background cycle that checks every agent on a configurable interval (default: 15 minutes).

## Architecture

The daemon is built on [APScheduler's](https://apscheduler.readthedocs.io/) `AsyncIOScheduler` and `IntervalTrigger`. This choice prevents the common mistake of building manual `asyncio.sleep()` loops, which can drift, silently fail on exceptions, and lack runtime introspection. APScheduler handles job persistence semantics, exception isolation, and rescheduling automatically.

The daemon can optionally accept an externally-managed `AsyncIOScheduler` instance (`scheduler` param). This matters because in production, PocketPaw may already have a scheduler running for reminders and other periodic tasks. By sharing a scheduler rather than spinning up a new one, the system avoids spawning redundant event loops.

```python
class HeartbeatDaemon:
    def __init__(
        self,
        interval_minutes: int = DEFAULT_HEARTBEAT_INTERVAL,
        scheduler: AsyncIOScheduler | None = None,
    ):
        self._owns_scheduler = scheduler is None
        ...
```

The `_owns_scheduler` flag tracks whether `HeartbeatDaemon` created the scheduler itself. On `stop()`, it only shuts down the scheduler if it owns it — preventing it from killing a shared scheduler that other jobs depend on.

## Staggered Wakeups

A naive implementation would wake all agents simultaneously at each interval tick. This creates a thundering-herd problem: if there are many agents, all of them hit the store layer at the same instant, potentially causing lock contention on the file-based Mission Control store. The daemon prevents this with a 2-second stagger between each agent wakeup:

```python
for i, agent in enumerate(agents):
    if i > 0:
        await asyncio.sleep(2)
    await self._wake_agent(agent.id)
```

## Work Detection

`_check_for_work()` queries two data sources: unread notifications (which represent @mentions and alerts) and assigned tasks. Unread notifications are treated as "urgent" — this status triggers `AgentStatus.ACTIVE` rather than `AgentStatus.IDLE`. The distinction matters for dashboard visibility and future prioritization logic.

## Callback Protocol

The daemon supports an optional callback function, which can be sync or async. The coroutine check (`asyncio.iscoroutinefunction`) prevents the common bug of calling `await` on a sync function or forgetting to `await` a coroutine. Callback failures are caught and logged without killing the heartbeat cycle — a failed callback should never silently stop all future wakeups.

## Singleton Reset for Testing

`reset_heartbeat_daemon()` is explicitly exported to allow test suites to tear down and rebuild the singleton between tests. Without this, tests that spin up a daemon could leave a background job running across test cases, causing state pollution.

## Known Gaps

- **Status resolution is coarse**: Both "has work" and "has urgent work" currently resolve to `AgentStatus.IDLE` unless there is urgent work. There is no distinct "backlogged" status.
- **`trigger_heartbeat()` is defined but not wired**: The public `trigger_heartbeat()` method allows callers to manually wake a specific agent immediately. It is implemented but no current call site uses it.
- **No cross-process coordination**: The singleton is process-local. Multiple PocketPaw processes each maintain their own daemon.