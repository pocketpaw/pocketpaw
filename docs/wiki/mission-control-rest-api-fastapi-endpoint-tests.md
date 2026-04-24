---
{
  "title": "Mission Control REST API: FastAPI Endpoint Tests",
  "summary": "Tests for the FastAPI router that exposes Mission Control functionality over HTTP, covering agent, task, message, document, activity, stats, and notification endpoints. Uses FastAPI's TestClient with monkeypatched singletons for full request/response validation without a live server.",
  "concepts": [
    "FastAPI",
    "TestClient",
    "Mission Control API",
    "REST endpoints",
    "agent CRUD",
    "task management",
    "notifications",
    "activity feed",
    "singleton injection",
    "monkeypatch",
    "standup report",
    "document API"
  ],
  "categories": [
    "API",
    "multi-agent",
    "testing",
    "orchestration",
    "test"
  ],
  "source_docs": [
    "ebac7f9fd2f5fa3b"
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

`test_mission_control_api.py` exercises the Mission Control HTTP layer — the FastAPI router that external tools, dashboards, and other agents use to interact with the orchestration system. Every CRUD operation for agents, tasks, messages, and documents is covered, along with activity feeds, stats, and notifications.

## Test Setup and Singleton Injection

The fixture creates a real `FastAPI` app, mounts the Mission Control `router`, and monkeypatches the module-level `_store_instance` and `_manager_instance` variables to point at test instances backed by a `tempfile.TemporaryDirectory`. This approach — injecting at the module attribute level rather than via a dependency override — tests the same singleton access pattern used by the actual application, so any module-level caching bug will surface here.

The `TestClient` from FastAPI runs requests synchronously in the test process, removing the need for a running ASGI server.

## Agent Endpoints (`TestAgentAPI`)

- `test_list_agents_empty`: `GET /agents` returns `[]` when no agents exist, not `404` or an error.
- `test_create_agent`: `POST /agents` persists a new agent and returns the created profile with an assigned ID.
- `test_get_agent` / `test_get_agent_not_found`: `GET /agents/{id}` returns the agent or a proper `404` with an error body. A missing agent must not return `500` — 404 is the contract.
- `test_update_agent`: `PUT /agents/{id}` applies partial updates. The test confirms only the provided fields change and others are preserved.
- `test_delete_agent`: `DELETE /agents/{id}` removes the agent and returns success. Subsequent GET returns `404`.
- `test_record_heartbeat`: `POST /agents/{id}/heartbeat` records the timestamp without requiring a request body. This keeps the heartbeat call as lightweight as possible for frequent polling.

## Task Endpoints (`TestTaskAPI`)

- `test_create_task_with_assignees`: Creating a task with `assignees` sends notifications to those agents. The test verifies the notification is stored after task creation.
- `test_get_task_with_messages`: `GET /tasks/{id}` returns the task along with its message thread, enabling a single API call to render the full task view.
- `test_filter_tasks_by_status`: `GET /tasks?status=pending` filters the result set. Without this, dashboards would need to fetch all tasks and filter client-side, which does not scale.
- `test_assign_task`: `POST /tasks/{id}/assign` adds assignees to an existing task and notifies them.

## Message Endpoints (`TestMessageAPI`)

- `test_post_message_with_mentions`: Posting a message with `@mentions` triggers notifications. The test asserts both the message is stored and the notifications appear in the mentioned agents' queues.
- `test_get_messages_for_task`: Returns the ordered message thread for a task. Order matters — agents process messages sequentially.

## Document Endpoints (`TestDocumentAPI`)

- `test_list_documents_filtered`: Documents can be filtered by type (e.g., `REPORT`, `PLAN`). This supports specialized views in the dashboard without transferring all documents.

## Activity and Stats (`TestActivityStatsAPI`)

- `test_activity_feed`: Returns a chronological stream of events across all agents and tasks for audit logging and the dashboard timeline.
- `test_stats`: Returns aggregate counts (agents, tasks by status). Used by the dashboard overview widget.
- `test_standup`: `GET /standup` generates a standup report. The test confirms the response contains per-agent summaries without crashing when some agents have no recent activity.

## Notification Endpoints (`TestNotificationAPI`)

Notifications accumulate for each agent until explicitly read. The endpoint tests confirm that unread counts are accurate and that marking notifications read clears them from the queue, preventing repeat delivery.

## Known Gaps

No TODOs in the file. The test suite uses synchronous `TestClient` exclusively — asynchronous race conditions in the API (e.g., concurrent task assignment and status update) are not tested here.
