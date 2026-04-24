---
{
  "title": "Deep Work Session — Full Project Lifecycle Orchestrator",
  "summary": "DeepWorkSession is the top-level coordinator for a Deep Work project, tying together GoalParser, PlannerAgent, DependencyScheduler, MCTaskExecutor, and HumanTaskRouter into a single class that manages a project from user input through goal analysis, LLM planning, approval, execution, pause, resume, and cancellation. It also handles crash recovery for interrupted projects on startup.",
  "concepts": [
    "DeepWorkSession",
    "project lifecycle",
    "start",
    "approve",
    "pause",
    "resume",
    "cancel",
    "recover_interrupted_projects",
    "materialize_tasks",
    "asyncio.Lock",
    "GoalParser integration",
    "phase broadcasting",
    "health engine"
  ],
  "categories": [
    "deep-work",
    "orchestration",
    "lifecycle-management",
    "session-management"
  ],
  "source_docs": [
    "a4452e5d36d44083"
  ],
  "backlinks": null,
  "word_count": 606,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`DeepWorkSession` is the facade that external callers (the API layer, the dashboard) interact with. It doesn't implement planning or scheduling logic directly — it coordinates the specialized components that do. Its job is lifecycle management: translating user actions (start, approve, pause, resume, cancel) into the right sequence of component calls, persisting state through Mission Control, and broadcasting phase events to the UI.

## Component Wiring

The session accepts all components via constructor injection, with defaults:

```python
def __init__(self, manager, executor, planner=None, scheduler=None, human_router=None):
    self.planner = planner or PlannerAgent(manager)
    self.human_router = human_router or HumanTaskRouter()
    self.scheduler = scheduler or DependencyScheduler(manager, executor, self.human_router)
```

Defaulting to real implementations while accepting injected ones makes unit testing straightforward — tests inject mocks, production code uses defaults.

## `start` — Creating and Planning a Project

`start(user_input, research_depth)` is the entry point for a new project:

1. Creates a `Project` with status `DRAFT`
2. Runs `GoalParser.parse(user_input)` to produce a `GoalAnalysis`
3. Transitions the project to `PLANNING`
4. Calls `plan_existing_project` to run the full planning pipeline
5. Returns the project in `AWAITING_APPROVAL` status

The GoalParser step (added 2026-02-18) runs before the planner so the planner receives structured context (domain, complexity, research depth recommendation) instead of raw user text.

## `plan_existing_project` and Locking

`plan_existing_project` delegates to `_plan_existing_project_locked`, which wraps the planning call in an `asyncio.Lock`. The lock prevents concurrent planning calls on the same project — without it, two simultaneous API requests could both enter the planning phase, producing duplicate PRDs and task sets.

Inside the locked call, the planner runs all four phases, then `_materialize_tasks` converts `TaskSpec` objects into real MC Tasks, creating the agent assignments.

## `_materialize_tasks` — Planning to Execution Bridge

This private method is the bridge between the planning layer (PlannerResult) and the execution layer (MC Tasks). For each `TaskSpec`:

1. Creates an MC Task with the spec's title, description, and role
2. Copies `max_retries` and `timeout_minutes` from the spec (added v2)
3. Assigns to the matched agent from `AgentSpec` recommendations
4. Records the `key → task_id` mapping for dependency resolution

After all tasks are created, it patches `blocked_by` on each task using the key-to-ID map. The two-pass approach (create all, then link) prevents forward-reference failures.

## Lifecycle Methods

- **`approve(project_id)`** — transitions to `EXECUTING`, calls `scheduler.get_ready_tasks()` and dispatches the first wave
- **`pause(project_id)`** — transitions to `PAUSED`, calls `executor.stop_project_tasks()` to halt running agent loops
- **`resume(project_id)`** — transitions back to `EXECUTING`, re-dispatches all currently ready tasks
- **`cancel(project_id)`** — transitions to `CANCELLED`, stops all running tasks, marks pending tasks `SKIPPED`, broadcasts `dw_project_cancelled`

## `recover_interrupted_projects`

Called at dashboard startup. Queries Mission Control for projects in `EXECUTING` status — these are projects that were mid-execution when the server last stopped. For each, it calls `scheduler.get_ready_tasks()` and re-dispatches, resuming work without requiring user action. Returns the count of recovered projects.

Without recovery, any server restart would leave in-progress projects stalled in `EXECUTING` status indefinitely.

## Error Recording

Planning errors are recorded to the health engine's `ErrorStore` (added 2026-02-17). If the LLM call fails, or JSON parsing fails, the error is stored with a `"deep_work_planning"` category. The `--doctor` CLI command surfaces these errors in the health report.

## Known Gaps

- The asyncio lock is per-session instance, not per-project-ID. If two session instances exist (e.g., multiple workers), concurrent planning is not prevented.
- `_extract_title()` uses a simple heuristic (first heading line in the PRD) and falls back to the raw user input if no heading is found — very long user inputs would produce unwieldy project titles.
- `subscribe_to_bus()` must be called explicitly to wire bus event handling; forgetting this call means `_on_system_event` never fires and task completion events are not processed.
