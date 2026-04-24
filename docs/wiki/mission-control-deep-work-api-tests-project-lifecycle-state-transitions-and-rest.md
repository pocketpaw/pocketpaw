---
{
  "title": "Mission Control (Deep Work) API Tests: Project Lifecycle, State Transitions, and REST Endpoints",
  "summary": "This test module, created in February 2026, covers PocketPaw's Mission Control subsystem — the project and task management layer that tracks long-horizon AI work. It validates the full project lifecycle through both the `MissionControlManager` directly and the FastAPI REST endpoints, including state transitions (draft, approved, paused, resumed) and proper 404 handling for missing resources.",
  "concepts": [
    "MissionControlManager",
    "FileMissionControlStore",
    "project lifecycle",
    "TaskStatus",
    "state transitions",
    "approve",
    "pause",
    "resume",
    "FastAPI router",
    "TestClient",
    "singleton reset",
    "progress calculation",
    "creator_id"
  ],
  "categories": [
    "testing",
    "project management",
    "REST API",
    "agent runtime",
    "test"
  ],
  "source_docs": [
    "b559b4e42ea6cac6"
  ],
  "backlinks": null,
  "word_count": 547,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Mission Control (also called Deep Work) allows agents to manage long-running projects with tasks, statuses, and progress tracking. This is distinct from the reactive message-response loop — it's a durable project management layer that persists across sessions. The tests validate both the manager's business logic and the HTTP API surface that the dashboard and external integrations consume.

## Fixture Architecture

The test setup uses a clean isolation pattern:
1. `reset_mission_control_store()` and `reset_mission_control_manager()` clear any singleton instances from previous tests.
2. A `FileMissionControlStore` is created against a `TemporaryDirectory`.
3. A `MissionControlManager` is constructed with the fresh store.
4. Both are injected into their respective module-level singletons via `monkeypatch.setattr`, so the FastAPI router (which calls module-level getter functions) sees the test instances.
5. A fresh `FastAPI` app with the router mounted at `/api/mission-control` is constructed per test.

This pattern avoids inter-test pollution from shared singleton state, which is a common source of flaky tests in systems with module-level caches.

## Manager Tests (`TestProjectManager`)

### Creation
`test_create_project` verifies that `manager.create_project(title, description, tags)` returns a project with:
- Non-None `id`
- Correct `title`, `description`, and `tags`
- `creator_id == "human"` (distinguishing human-created projects from agent-created ones)
- `status.value == "draft"` (the initial state for all new projects)

### Read
`test_get_project` creates a project and retrieves it by ID, confirming persistence. `test_get_project_not_found` asserts `None` is returned (not an exception) for an unknown ID, allowing callers to handle missing resources gracefully.

### Listing and Filtering
`test_list_projects` creates multiple projects and asserts all appear in the list. `test_list_projects_by_status` creates projects in different states and confirms the status filter returns only matching entries — essential for the dashboard's project views (e.g., "show only active projects").

### Tasks and Progress
`test_get_project_tasks` verifies task retrieval is scoped to the parent project. `test_get_project_progress` asserts the progress calculation returns a dict with expected keys. `test_get_project_progress_empty` handles the edge case of a project with no tasks — the progress dict must not raise a `ZeroDivisionError`.

### Deletion
`test_delete_project` deletes a project and confirms it is no longer retrievable. `test_delete_project_not_found` asserts deletion of a non-existent project raises the appropriate exception (or returns a failure result, depending on the manager's error contract).

## API Tests (`TestProjectAPI`)

The API tests use `TestClient` for synchronous HTTP testing. They exercise the full HTTP stack:

- **Create** (`POST /api/mission-control/projects`): asserts 200/201 status and correct response body.
- **Validation** (`test_create_project_validation`): missing required fields return 422 Unprocessable Entity.
- **List** with and without status filter.
- **Get** by ID: 200 for existing, 404 for missing.
- **Update** (`PATCH`): partial field updates are reflected in subsequent GET responses.
- **Status update**: changing `status` field via PATCH.
- **State transitions**: dedicated endpoints for `approve`, `pause`, and `resume` actions on projects. Each has a corresponding 404 test for missing projects.
- **Delete** (`DELETE`): 200 for success, 404 for missing.

The state transition endpoints (`/approve`, `/pause`, `/resume`) test PocketPaw's command-style API design: complex state changes are not raw PATCH operations but named commands with their own endpoints, making the state machine explicit and auditable.

## Known Gaps

No TODO or FIXME markers are present. The `TaskStatus` model is imported but tasks are not directly created or updated via the API in this test file — task lifecycle (create, update, complete) is likely covered in a separate task-specific test file.