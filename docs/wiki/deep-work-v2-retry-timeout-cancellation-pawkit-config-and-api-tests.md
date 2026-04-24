---
{
  "title": "Deep Work v2: Retry, Timeout, Cancellation, PawKit Config, and API Tests",
  "summary": "This suite covers the v2 Deep Work feature additions: Task and TaskSpec retry/timeout fields, the ProjectStatus.CANCELLED state, the PawKit configuration schema (panels, sections, layouts, workflows), YAML round-trips, executor output storage and auto-retry logic, session cancellation, and the cancel/retry REST API endpoints.",
  "concepts": [
    "Task_v2_fields",
    "retry_count",
    "max_retries",
    "timeout_minutes",
    "output_storage",
    "error_message",
    "ProjectStatus_CANCELLED",
    "PawKit",
    "PawKitConfig",
    "PanelConfig",
    "LayoutConfig",
    "WorkflowConfig",
    "MCTaskExecutor_retry",
    "DeepWorkSession_cancel",
    "Deep_Work_v2"
  ],
  "categories": [
    "testing",
    "deep-work",
    "v2-features",
    "dashboard-config",
    "cancellation",
    "test"
  ],
  "source_docs": [
    "1e39242ec8dab73c"
  ],
  "backlinks": null,
  "word_count": 557,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_v2.py` tests the second-generation Deep Work features added in the 2026-02-26 iteration. These additions hardened the executor with retry/timeout semantics, introduced a structured dashboard configuration format (PawKit), and added project cancellation as a first-class operation.

## Task v2 Fields

`TestTaskV2Fields` tests five new fields added to the `Task` model:

- **output**: Stores the agent's final result text. Without this field, task outputs were lost on restart — agents would re-run already-completed work.
- **retry_count**: Tracks how many times the task has been attempted. Incremented by the executor on each failed run.
- **max_retries**: Maximum allowed retry attempts. When `retry_count >= max_retries`, the executor marks the task as permanently failed rather than retrying indefinitely.
- **timeout_minutes**: Maximum wall-clock time before the executor kills the agent process and marks the task as timed out.
- **error_message**: Stores the failure reason for failed tasks, visible in the dashboard.

Backward compatibility tests confirm tasks saved before v2 (without these fields) load correctly with safe defaults: `retry_count=0`, `max_retries=0` (no retries), `timeout_minutes=None` (no timeout), `output=None`, `error_message=None`.

## TaskSpec v2 Fields

`TestTaskSpecV2Fields` mirrors the above for `TaskSpec` (the planner output format), confirming that the planner can now specify per-task retry and timeout budgets in its LLM-generated plan.

## ProjectStatus.CANCELLED

`TestProjectStatusCancelled` verifies the new `CANCELLED` enum value. Tests confirm it exists, has the expected string value, can be reconstructed from a string (for deserialization), is distinct from `COMPLETED`, and is treated as a terminal state alongside `COMPLETED`. The terminal-state classification matters for the scheduler — terminal projects are not re-dispatched.

## PawKit Configuration Schema

PawKit is PocketPaw's YAML-based dashboard configuration format. The v2 suite introduces tests for the full schema:

- **PawKitMeta**: App metadata (name, description, version, category).
- **PanelConfig**: Individual dashboard panels — table, kanban, metrics row, chart (with type and period), and feed (with max items).
- **SectionConfig**: Groups panels with configurable column span.
- **LayoutConfig**: Arranges sections into a dashboard page.
- **WorkflowConfig / WorkflowTrigger**: Declarative automation rules that fire on schedule or event triggers.
- **UserConfigField**: User-configurable settings exposed in the dashboard UI.
- **IntegrationRequirements**: Declares required external integrations (e.g., GitHub, Slack).

## PawKit YAML Round-Trips

Tests verify `load_pawkit_from_string()`, `save_pawkit()`, and `load_pawkit()` correctly serialize and deserialize the full `PawKitConfig` object to/from YAML. This ensures that PawKit app definitions authored by developers survive the load/save cycle without data loss.

## MCTaskExecutor v2 Behavior

Tests cover the executor's new capabilities:

- **Output storage**: Agent output is saved to `task.output` after successful completion.
- **Timeout enforcement**: A task exceeding `timeout_minutes` is killed and marked FAILED with an appropriate error message.
- **Auto-retry**: On failure, if `retry_count < max_retries`, the executor increments the counter and re-dispatches.
- **Retry exhaustion**: When retries are exhausted, the task is permanently marked FAILED.
- **stop_all_project_tasks**: Used by pause and cancel to halt all running tasks for a project.

## DeepWorkSession.cancel()

`cancel()` is a new session method that: sets project status to CANCELLED, skips all INBOX/PENDING tasks (marks them CANCELLED), stops all RUNNING tasks via the executor, and rejects cancellation attempts on already-terminal projects (COMPLETED or already CANCELLED).

## REST API Endpoints

`POST /projects/{id}/cancel` and `POST /projects/{id}/tasks/{tid}/retry` are tested via FastAPI `TestClient`. These expose the v2 capabilities to the dashboard and CLI consumers.

## Known Gaps

PawKit workflow execution (what happens when a `WorkflowTrigger` fires) is not tested here — the tests only cover schema creation and serialization.
