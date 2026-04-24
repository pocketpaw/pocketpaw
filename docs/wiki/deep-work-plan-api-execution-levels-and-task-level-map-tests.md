---
{
  "title": "Deep Work Plan API: Execution Levels and Task Level Map Tests",
  "summary": "This suite tests the GET /projects/{id}/plan endpoint, which returns a project's dependency graph as parallel execution levels — groups of tasks that can run concurrently because their upstream dependencies are satisfied. It covers linear chains, diamond-shaped dependency graphs, and fully independent task sets using a real FastAPI TestClient with an in-memory store.",
  "concepts": [
    "execution_levels",
    "task_level_map",
    "GET_projects_plan",
    "dependency_graph",
    "topological_sort",
    "parallelism",
    "diamond_graph",
    "linear_chain",
    "independent_tasks",
    "FastAPI_TestClient",
    "FileMissionControlStore",
    "MissionControlManager",
    "Deep_Work_API",
    "deep_work_router"
  ],
  "categories": [
    "testing",
    "deep-work",
    "REST-API",
    "dependency-graph",
    "parallelism",
    "test"
  ],
  "source_docs": [
    "e0dd16e18bb809d1"
  ],
  "backlinks": null,
  "word_count": 499,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_execution_levels.py` tests the `GET /projects/{id}/plan` REST endpoint in `pocketpaw.deep_work.api`. This endpoint exposes the project's dependency graph as a serialized execution plan — a list of "levels" where tasks within the same level can run in parallel, and tasks in level N must wait for all tasks in level N-1 to complete.

## Why This Module Exists

The execution level structure is what transforms a flat list of tasks with dependency annotations into a visual and operational workflow. The dashboard uses it to render a Gantt-like plan view. The scheduler uses the level grouping to maximize parallelism. Human operators reviewing the plan need it to understand the critical path and estimated total duration.

## Test Setup

`test_app` creates a full FastAPI application with both the `mc_router` (mission control CRUD endpoints) and `deep_work_router` (planning and execution endpoints) mounted. It uses `monkeypatch` to inject real `FileMissionControlStore` and `MissionControlManager` instances pointing at a temporary directory, then wraps it in FastAPI's `TestClient` for synchronous HTTP testing.

This integration approach (real store + real router + test client) is more thorough than mocking the store — it catches serialization bugs that a mock would hide.

## Linear Chain

`test_linear_chain` creates three tasks: A has no blockers, B is blocked by A, and C is blocked by B. The plan endpoint should return `execution_levels = [[A.id], [B.id], [C.id]]` — three sequential levels with no parallelism possible. This is the simplest DAG: a single critical path.

## Diamond Graph

`test_diamond_graph` creates a diamond-shaped dependency: A unblocks both B and C, and D requires both B and C. The expected execution levels are `[[A.id], [B.id, C.id], [D.id]]`. The middle level contains both B and C because they have no dependency on each other — they can execute concurrently after A finishes, then D waits for both to complete before starting.

This test is critical because it validates the parallelism detection logic. If the scheduler incorrectly serialized B and C, projects would take twice as long as necessary.

## Independent Tasks

`test_independent_tasks` creates three tasks with no dependencies between them. All three should appear in a single execution level — maximum parallelism. This represents the case where a planner generates work items with no ordering constraints.

## task_level_map

In addition to the `execution_levels` array, the endpoint also returns a `task_level_map` — a dictionary mapping each task ID to its level index. This flat lookup structure is used by the dashboard to annotate each task card with its level without iterating the full levels array.

Tests verify both structures are consistent: a task in `execution_levels[1]` should have `task_level_map[task.id] == 1`.

## Status Filtering

Tasks with DONE status are present in the graph for dependency resolution but may be visually de-emphasized in the plan view. The tests use INBOX-status tasks to represent pending work.

## Known Gaps

The endpoint is not tested for very large graphs (100+ tasks) where topological sort performance could become observable. Error cases (project not found, corrupted task data) are not covered in this suite.
