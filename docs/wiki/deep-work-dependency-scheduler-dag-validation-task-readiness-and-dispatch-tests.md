---
{
  "title": "Deep Work Dependency Scheduler: DAG Validation, Task Readiness, and Dispatch Tests",
  "summary": "This suite validates the DependencyScheduler, the core orchestration engine that determines which tasks are ready to run based on dependency graphs, dispatches agent and human tasks to the correct handlers, and detects project completion. It also tests DAG validation logic that prevents cyclic dependencies and missing references from corrupting project execution.",
  "concepts": [
    "DependencyScheduler",
    "get_ready_tasks",
    "on_task_completed",
    "validate_graph",
    "get_execution_order",
    "DAG_validation",
    "cycle_detection",
    "task_dispatch",
    "project_completion",
    "blocked_by",
    "execute_task_background",
    "HumanTaskRouter",
    "topological_sort",
    "Deep_Work"
  ],
  "categories": [
    "testing",
    "deep-work",
    "scheduling",
    "dependency-graph",
    "task-dispatch",
    "test"
  ],
  "source_docs": [
    "c206a198282e3f77"
  ],
  "backlinks": null,
  "word_count": 508,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_scheduler.py` tests `pocketpaw.deep_work.scheduler.DependencyScheduler`, which sits at the heart of PocketPaw's Deep Work execution engine. The scheduler continuously monitors task state, unlocks tasks whose dependencies are satisfied, routes them to the right executor (AI agent or human), and signals project completion.

## Why This Module Exists

A multi-task project with explicit dependencies requires a runtime engine to enforce ordering — you cannot run "Deploy to production" before "Write tests" completes. The `DependencyScheduler` implements this as a DAG (directed acyclic graph) traversal, polling task status and dispatching ready work. Without this, the Deep Work system would require manual task sequencing.

## get_ready_tasks

`TestGetReadyTasks` covers the core readiness check:

- `test_returns_tasks_with_all_blockers_done`: A task whose entire `blocked_by` list contains only DONE tasks is ready for dispatch.
- `test_excludes_tasks_with_incomplete_blockers`: If any upstream blocker is not yet DONE, the task stays queued.
- `test_excludes_already_running_tasks`: The scheduler checks `executor.is_task_running()` to avoid double-dispatching a task that is already in progress. Without this guard, a task could be dispatched twice, causing duplicate work or race conditions.
- `test_returns_tasks_with_no_blockers`: Tasks with an empty `blocked_by` list are immediately ready.
- `test_filters_by_project_id`: Readiness queries are scoped to a single project, preventing cross-project interference.

## on_task_completed

`TestOnTaskCompleted` tests what happens when a task transitions to DONE:

- `test_dispatches_agent_task`: Agent tasks (`task_type="agent"`) are routed to `executor.execute_task_background()`.
- `test_routes_human_task`: Human tasks (`task_type="human"`) trigger the `HumanTaskRouter` rather than the executor.
- `test_routes_review_task`: Review tasks also route to the human router with a distinct notification type.
- `test_detects_project_completion`: When all tasks in a project are DONE, the scheduler updates `project.status` to COMPLETED.
- `test_no_dispatch_when_task_has_no_project`: Tasks without a `project_id` are ignored, preventing null-reference errors.

## DAG Validation — validate_graph

`TestValidateGraphTask` and `TestValidateGraphTaskSpec` test the graph validation function against both `Task` (runtime objects) and `TaskSpec` (planner output objects):

- **Linear chain (A→B→C)**: Valid, no errors.
- **Diamond (A→B→D, A→C→D)**: Valid DAG with merge point.
- **Simple cycle (A→B→A)**: Detected and rejected. Without this check, the scheduler would spin forever dispatching tasks that block each other.
- **Complex cycle (A→B→C→A)**: Also detected.
- **Non-existent reference**: A task that references an ID not in the graph is caught. This prevents silent dead-locks where a task waits forever for a dependency that doesn't exist.
- **Empty list**: Trivially valid.

## Execution Order — get_execution_order

`TestGetExecutionOrderTask` tests the topological sort that groups tasks into parallel execution levels:

- Linear chain → three levels: `[A]`, `[B]`, `[C]`.
- Diamond → two levels: `[A]`, `[B, C]` (B and C can run in parallel), `[D]`.
- Independent tasks → all in level one (maximum parallelism).

The level grouping is exposed via the API's `/projects/{id}/plan` endpoint so operators can visualize the execution wave plan.

## Test Infrastructure

Fixtures create mock `MissionControlManager`, `MCTaskExecutor`, and `HumanTaskRouter` objects. The scheduler is constructed with all three injected as dependencies, keeping tests isolated from file I/O and LLM calls.

## Known Gaps

The scheduler's behavior under task failure and retry is not covered here — that falls under the v2 tests. There are no tests for scheduler behavior when the manager raises exceptions (e.g., database connection failures mid-dispatch).
