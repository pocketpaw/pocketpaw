---
{
  "title": "Task Status Persistence Tests: Reproducing and Fixing a FastAPI Body vs Query Param Bug",
  "summary": "Tests that reproduce and validate the fix for a bug where task status updates via the API did not persist — caused by FastAPI treating a bare `str` parameter as a query param while the frontend sent it as a JSON body. Also validates that `projectTasks` lists are updated alongside the main tasks list.",
  "concepts": [
    "FileMissionControlStore",
    "MissionControlManager",
    "FastAPI Body vs query param",
    "task status persistence",
    "SKIPPED status",
    "projectTasks sync",
    "TestClient",
    "regression test"
  ],
  "categories": [
    "testing",
    "bug reproduction",
    "API",
    "deep-work",
    "test"
  ],
  "source_docs": [
    "461a689646f1d943"
  ],
  "backlinks": null,
  "word_count": 424,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

This test file was written first as a bug reproduction, then retained as a regression guard. The bug: calling `POST /tasks/{id}/status` with a JSON body `{"status": "done"}` did not persist the status change. The fix: FastAPI treats a bare `str` endpoint parameter as a query parameter, not a request body. The endpoint signature needed to use a `Body()` annotation.

## Bug Context

The comment at the top of the file explains the root cause precisely: `update_task_status` declared `status: str` as a plain parameter. FastAPI's routing logic maps this to a query parameter (`?status=done`). The frontend (and any API client following REST conventions) sent the status in the JSON body. The mismatch meant the parameter was `None` at the handler, status never changed, and the GET response always showed the original status.

## Test Structure

A `TestClient`-based test app is constructed with both the `deep_work_router` and `mission_control_router` mounted. Fixtures provision a real `FileMissionControlStore` backed by a `tempfile.TemporaryDirectory`, ensuring tests use actual persistence semantics rather than an in-memory mock.

## Core Persistence Test

`test_status_update_via_json_body` directly reproduces the bug scenario: POST with a JSON body, then GET and confirm the status persisted. This is the canonical regression test.

## Timestamps

`test_status_update_sets_completed_at` verifies that transitioning to `done` sets `completed_at` to a non-null timestamp. Missing `completed_at` breaks duration reporting in the dashboard.

## SKIPPED Status

`test_status_update_to_skipped_via_json_body` confirms the fix works for all status values, not just `done`. Because SKIPPED was added in a later sprint, verifying it separately guards against status-specific routing logic.

## Round-Trip

`test_status_update_round_trip` performs create → update → retrieve, asserting the final state matches the updated value. This is the full persistence contract in one test.

## Validation

`test_status_update_invalid_status_returns_error` confirms that sending an invalid status string (not in the `TaskStatus` enum) returns an HTTP error rather than silently accepting and storing bad data.

## Project Task Sync

`test_project_task_status_persists_after_refetch` caught a secondary bug: task status was updated in the main tasks list but `projectTasks` (the per-project task view) still showed the old status. The async `link()` helper in the test creates the task-project association before testing the update path.

## Known Gaps

The test uses `run_coro(coro)` to drive async operations within a synchronous `TestClient` context, suggesting the test was adapted from async to sync to work with `TestClient`. An async test client (`httpx.AsyncClient`) would be cleaner.

```python
# Root cause fix (simplified)
# Before: status: str  # FastAPI treats as query param
# After:  status: str = Body(...)  # FastAPI reads from JSON body
@router.post("/tasks/{task_id}/status")
async def update_task_status(task_id: str, status: str = Body(...)):
    ...
```
