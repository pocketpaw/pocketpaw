---
{
  "title": "Mission Control API — FastAPI Router for Agent Orchestration Endpoints",
  "summary": "`api.py` defines the FastAPI router for Mission Control, exposing REST endpoints for agents, tasks, messages, documents, activity, notifications, and Deep Work projects, with Pydantic validation on all write requests and business logic delegated to `MissionControlManager`.",
  "concepts": [
    "FastAPI router",
    "Pydantic validation",
    "UUID validation",
    "task execution API",
    "Deep Work projects",
    "_enrich_project_dict",
    "notification retrieval",
    "idempotency",
    "mission_control_router",
    "WebSocket streaming endpoints"
  ],
  "categories": [
    "Multi-Agent Orchestration",
    "REST API"
  ],
  "source_docs": [
    "40dd340dcc40348d"
  ],
  "backlinks": null,
  "word_count": 433,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

MC's REST API is the interface between the PocketPaw dashboard (and external tools) and the Mission Control backend. It was created on 2026-02-05 and extended on 2026-02-12 with project management and task-project association.

Mount with:

```python
app.include_router(mission_control_router, prefix="/api/mission-control")
```

## Request Validation with Pydantic

All write endpoints accept Pydantic `BaseModel` request bodies with field-level constraints:

```python
class RunTaskRequest(BaseModel):
    agent_id: str = Field(
        ...,
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-...$",
    )
```

The UUID pattern validator in `RunTaskRequest` prevents obviously malformed IDs from reaching the executor, catching programming errors in dashboard clients early. Without this, a malformed ID would either silently fail a lookup or cause an unhandled exception deeper in the stack.

## Task Execution Endpoints

```
POST /tasks/{id}/run   -> Start background execution (409 if already running)
POST /tasks/{id}/stop  -> Cancel running execution
GET  /tasks/running    -> List active executions
```

The `/run` endpoint returns immediately with `{"status": "started", ...}`; actual execution streams via WebSocket. Checking `executor.is_task_running(task_id)` before dispatching prevents double-execution, which would spawn two competing agent processes on the same task.

## Deep Work Project Endpoints

Projects (`/projects`) represent longer-horizon work spanning multiple tasks. The MC API provides simple CRUD and status lifecycle endpoints (`/approve`, `/pause`, `/resume`). For full orchestration (dispatching tasks to agents on approval), callers are directed to the Deep Work API (`/api/deep-work/...`).

`_enrich_project_dict` adds `folder_path` and `file_count` to project responses, used by the dashboard's sidebar project browser to show file system metadata alongside project data without coupling the models layer to path logic.

## Notification Retrieval Quirk

```python
# Get all notifications (hacky but works for now)
notifications = list(manager._store._notifications.values())
```

When no `agent_id` filter is provided, the endpoint accesses the store's internal `_notifications` dict directly. This is acknowledged in the code as a workaround — the store lacks a `list_all_notifications()` method.

## Status Update Pattern

The `PATCH /tasks/{id}` endpoint handles both field updates and status changes in one payload. However, status changes are routed through `manager.update_task_status` (which updates timestamps and logs activity), while field updates go directly to `manager._store.save_task`. This split exists because status transitions have side effects (activity logging, timestamp recording) that must not be bypassed, while simple field changes like renaming a task do not.

The `POST /tasks/{id}/status` endpoint exists as a dedicated status-only endpoint for clients that want to update status without sending a full task payload. Both paths converge on the same `update_task_status` method.

## Known Gaps

Direct store access (`manager._store.delete_task`, `manager._store._notifications`) bypasses the manager's business logic (activity logging, event emission) for some operations. Project status transitions via the MC API skip orchestration side-effects (task dispatch, agent notification) that only happen via the Deep Work API.