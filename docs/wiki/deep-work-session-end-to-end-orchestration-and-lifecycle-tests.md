---
{
  "title": "Deep Work Session: End-to-End Orchestration and Lifecycle Tests",
  "summary": "This integration test suite covers the DeepWorkSession, the top-level orchestrator that coordinates planning, task materialization, agent assignment, approval gating, pause/resume, and cancellation for multi-task AI projects. It verifies the full session lifecycle from DRAFT through PLANNING to AWAITING_APPROVAL and beyond, including edge cases like cyclic dependencies, empty plans, and planner exceptions.",
  "concepts": [
    "DeepWorkSession",
    "start",
    "approve",
    "pause",
    "resume",
    "cancel",
    "task_materialization",
    "inverse_blocks",
    "agent_assignment",
    "specialty_match",
    "PRD",
    "singleton",
    "system_event_routing",
    "mc_task_completed",
    "Deep_Work"
  ],
  "categories": [
    "testing",
    "deep-work",
    "session-orchestration",
    "integration-testing",
    "test"
  ],
  "source_docs": [
    "5d9574a67a23e811"
  ],
  "backlinks": null,
  "word_count": 586,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_session.py` tests `pocketpaw.deep_work.session.DeepWorkSession`, the orchestrator that ties together the planner, scheduler, task store, executor, and human router into a cohesive project execution workflow. This is one of the largest test files in the Deep Work system, mixing mock-based unit tests with real-store integration tests.

## Why This Module Exists

The Deep Work session is the public API surface for multi-task project management. It must correctly sequence: LLM-based planning → human approval → task dispatch → progress monitoring → completion/cancellation. Any failure in this coordination can result in orphaned tasks, double-dispatched work, or stuck projects.

## start() — Project Creation Flow

`TestStartCreatesProject` tests the happy path:

- **Creates project**: A `Project` record is persisted to the store with status DRAFT → PLANNING → AWAITING_APPROVAL.
- **Creates tasks**: Tasks from the planner's `TaskSpec` list are materialized as real `Task` objects with correct `project_id`, `task_type`, and `blocked_by` fields.
- **Sets inverse blocks**: When task A blocks task B, the session sets `A.blocks = [..., B.id]`. This two-way linkage allows both forward (what I block) and reverse (what blocks me) traversal.
- **Creates agents**: `AgentSpec` objects from the planner are materialized as real agent records.
- **Assigns tasks**: Tasks are assigned to agents by specialty match — an agent with specialty `"python"` gets assigned Python-typed tasks.
- **Saves PRD**: The project requirements document generated during planning is persisted to the store and linked to the project.
- **Extracts title**: The session parses the first `#` heading from the PRD Markdown to use as the project title.

## start() — Edge Cases

`TestStartEdgeCases` covers failure modes:

- **Invalid graph**: If the planner returns a cyclic dependency graph, `start()` must reject it before persisting anything. This prevents the scheduler from entering an infinite dispatch loop.
- **Empty tasks**: If the planner returns no tasks, the session fails early with an informative error rather than creating an empty project.
- **Planner exception**: If the LLM call inside the planner raises (network error, context limit), `start()` cleans up and propagates the error rather than leaving a partial project in the store.
- **Reuses existing agent**: If an agent with the same specialty already exists in the store, the session reuses it rather than creating a duplicate.
- **Notifies plan ready**: After successful planning, `notify_plan_ready()` is called on the human router to alert the operator that approval is needed.

## approve()

`TestApprove` verifies that calling `approve(project_id)` transitions the project from AWAITING_APPROVAL to IN_PROGRESS and dispatches all tasks that are immediately ready (those with no blockers or whose blockers are already DONE).

`test_approve_not_found` confirms that approving a non-existent project raises a clear error.

## pause() and resume()

`TestPause` tests that `pause(project_id)` stops all currently running tasks via `executor.stop_all_project_tasks()` and updates the project status. The "no running tasks" case is also tested to confirm graceful behavior.

`TestResume` verifies that `resume(project_id)` re-dispatches all tasks that became ready during the pause period.

## Singleton Management

The session exposes `get_deep_work_session()` and `reset_deep_work_session()` singleton accessors. Tests verify that the singleton is created on first call and that `reset_deep_work_session()` tears it down for test isolation.

## System Event Routing

`test__on_system_event_routes_mc_task_completed` verifies that when the event bus emits an `mc_task_completed` event, the session correctly routes it to the scheduler's `on_task_completed()` method, closing the dispatch loop.

## Known Gaps

The `subscribe_to_bus` idempotency test confirms that calling `subscribe_to_bus()` twice does not register duplicate event listeners — a race condition that could cause tasks to be dispatched twice on reconnect. This is flagged in the source comments as a known concern for long-running sessions.
