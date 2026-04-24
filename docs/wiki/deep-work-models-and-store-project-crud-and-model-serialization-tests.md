---
{
  "title": "Deep Work Models and Store: Project CRUD and Model Serialization Tests",
  "summary": "This suite tests the Deep Work data models (Project, TaskSpec, AgentSpec, PlannerResult) and the FileMissionControlStore's project CRUD operations, ensuring correct defaults, serialization round-trips, persistence across store reinitializations, status filtering, and safe handling of missing records.",
  "concepts": [
    "Project",
    "TaskSpec",
    "AgentSpec",
    "PlannerResult",
    "FileMissionControlStore",
    "project_CRUD",
    "ProjectStatus",
    "to_dict",
    "from_dict",
    "round_trip",
    "persistence",
    "status_filter",
    "updated_at_timestamp",
    "reset_mission_control_store",
    "Deep_Work"
  ],
  "categories": [
    "testing",
    "deep-work",
    "persistence",
    "data-models",
    "file-store",
    "test"
  ],
  "source_docs": [
    "1f90d50e469b5336"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_store.py` tests two layers: the Deep Work data models that define the shape of project data, and the `FileMissionControlStore` that persists them to disk. Together these components form the persistence contract for the Deep Work system.

## Why This Module Exists

Deep Work projects may run for hours or days. The file store must reliably persist project state so that a process restart does not lose in-progress work. This suite guards the persistence layer against regressions in serialization logic and file I/O.

## Project Model

`TestProjectModel` tests the `Project` dataclass:

- **Defaults**: A bare `Project()` gets a UUID `id`, empty `title`, and `ProjectStatus.DRAFT` status. The auto-generated ID prevents accidental collisions between projects.
- **to_dict()**: All fields are serialized, including the `status` enum (as a string) and `tasks` list.
- **from_dict()**: A plain dict (as would be read from JSON on disk) reconstructs a valid `Project`.
- **Round-trip**: `to_dict()` → `from_dict()` produces an identical object.

## TaskSpec and AgentSpec Models

`TestTaskSpecModel` and `TestAgentSpecModel` follow the same pattern: defaults, round-trips, and serialization. These models represent planner output (what tasks should exist, what agents should be created) and are persisted alongside the project record for post-restart reference.

## PlannerResult Model

`TestPlannerResultModel` tests the container that holds all four phases of planner output (research notes, PRD, task specs, agent specs). Round-trip testing ensures none of the nested structures are lost during serialization.

## FileMissionControlStore — Project CRUD

`TestStoreProjectOperations` tests the full project lifecycle in the file store:

- **save_and_get**: Save a project, retrieve it by ID — confirms basic persistence works.
- **get_nonexistent**: Retrieving a non-existent project ID returns `None` rather than raising. This is the safe-navigation pattern used throughout the session manager.
- **list_projects**: Returns all persisted projects.
- **list_projects_with_status_filter**: Returns only projects matching a given status, enabling the API's status-filtered list endpoint.
- **delete_project**: Removes a project by ID, freeing disk space.
- **delete_nonexistent**: Deleting a non-existent ID is a no-op rather than an error.
- **save_project_updates_timestamp**: Each save bumps the `updated_at` timestamp. This ensures the dashboard shows accurate "last modified" times.
- **test_project_persistence**: Creates a store, saves a project, creates a NEW store instance pointing at the same directory, and retrieves the project. This confirms that data actually hits disk rather than living only in memory.
- **stats_include_projects**: The store's `get_stats()` endpoint includes project counts for the admin dashboard.
- **clear_all_includes_projects**: The `clear_all()` maintenance operation removes projects along with tasks and agents, preventing orphaned data.

## Test Infrastructure

Tests use a `temp_store_path` fixture that creates a real temporary directory and a `store` fixture that calls `reset_mission_control_store()` before creating a fresh `FileMissionControlStore`. The reset prevents test pollution from singleton store instances.

## Known Gaps

Concurrent write tests (two processes saving the same project simultaneously) are absent. The file store uses no locking, so concurrent writes could corrupt project JSON. This is a known architectural limitation for multi-process deployments.
