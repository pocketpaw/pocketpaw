---
{
  "title": "Deep Work Dependency Scheduler — DAG-Based Task Dispatch with Cycle Detection",
  "summary": "DependencyScheduler manages task dispatch order in a Deep Work project by treating tasks as a directed acyclic graph (DAG). It finds ready tasks (all blockers resolved), dispatches them concurrently, handles human task routing, validates for cycles via Kahn's algorithm, and optionally runs in a tick-synchronized mode driven by a SimulationClock.",
  "concepts": [
    "DependencyScheduler",
    "DAG",
    "task dispatch",
    "dependency resolution",
    "Kahn's algorithm",
    "cycle detection",
    "SKIPPED status",
    "asyncio.gather",
    "SimulationClock",
    "tick-synchronized dispatch",
    "get_ready_tasks",
    "execution order"
  ],
  "categories": [
    "deep-work",
    "scheduling",
    "orchestration",
    "graph-algorithms"
  ],
  "source_docs": [
    "63fdc803d0785338"
  ],
  "backlinks": null,
  "word_count": 570,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

In a Deep Work project, tasks often have dependencies — Task B can't start until Task A finishes. `DependencyScheduler` is the engine that tracks those dependencies and dispatches tasks at the right time. It answers three questions: which tasks are ready to run right now, which new tasks unblock when a task completes, and is there a cycle in the dependency graph that would deadlock the project?

## Readiness Check: `get_ready_tasks`

A task is "ready" when:
1. Its status is `INBOX` or `ASSIGNED` (not started, not finished)
2. All task IDs in its `blocked_by` list have status `DONE` or `SKIPPED`

The `SKIPPED` equivalence with `DONE` was added in the first update (2026-02-12). Before that, a skipped task left its dependents permanently blocked — if a human task was skipped, all downstream work stalled. Treating `SKIPPED` as resolved means the project can continue through optional or cancelled upstream tasks.

```python
resolved_ids = {
    t.id for t in project_tasks if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
}
```

## Cascade Dispatch: `on_task_completed`

When a task finishes, `on_task_completed` immediately re-evaluates all tasks in the same project. Any that are now unblocked get dispatched concurrently via `asyncio.gather`. This creates an event-driven cascade: finishing one task can unblock multiple parallel tasks, and they all start immediately without waiting for a scheduler tick.

## Task Routing: `_dispatch_task`

The dispatcher checks the task type:
- **Agent tasks** — routed to `executor.execute_task_background()`
- **Human tasks** — routed to `human_router.notify_human_task()` (if a `human_router` is configured)
- **Review tasks** — routed to `human_router.notify_review_task()`

The `human_router` is optional. If not configured, human tasks are dispatched to the executor anyway and the lack of notification is logged. This prevents the scheduler from crashing in environments without a messaging backend.

## Cycle Detection: `validate_graph`

`validate_graph` implements Kahn's algorithm over the task list. It computes in-degree for every node, seeds a queue with zero-in-degree nodes, and processes the queue — each processed node reduces the in-degree of its dependents. If after processing, some nodes remain unvisited, a cycle exists.

The algorithm works with both `Task` (`.id`, `.blocked_by`) and `TaskSpec` (`.key`, `.blocked_by_keys`) via the `_get_id()` and `_get_deps()` helper functions. This dual-type support lets cycle detection run during planning (on `TaskSpec` objects) before any MC Tasks are created, catching deadlocks before the user approves.

## Dependency Level Grouping: `get_execution_order`

`get_execution_order` groups tasks into levels (waves) using a BFS pass over the DAG. Level 0 tasks have no dependencies; Level 1 tasks depend only on Level 0; and so on. This produces a `list[list[str]]` where each inner list can execute in parallel.

This is used for estimation and display — showing the user that a 10-task project has 3 execution waves helps them understand expected parallelism.

## Tick-Synchronized Mode: `run_tick_synchronized`

Added 2026-03-26 (issue #633). When a `SimulationClock` is injected, `run_tick_synchronized` dispatches all ready tasks for the current tick, waits for them to complete, records a `TickSnapshot`, then advances the clock. This enables deterministic simulation of project execution — all tasks at the same dependency level run in the same simulated time unit.

The session layer decides whether to use real-time dispatch or tick-synchronized dispatch at initialization time.

## Known Gaps

- `on_task_completed` does not guard against concurrent calls for the same task. If two events arrive simultaneously for the same `task_id`, double-dispatch is possible.
- Cycle detection is not called automatically before execution begins — the session layer is responsible for invoking `validate_graph` after planning if desired.
