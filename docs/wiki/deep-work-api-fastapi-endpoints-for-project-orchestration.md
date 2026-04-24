---
{
  "title": "Deep Work API: FastAPI Endpoints for Project Orchestration",
  "summary": "deep_work/api.py defines the FastAPI router mounted at `/api/deep-work/*`, exposing REST endpoints for the full Deep Work project lifecycle — goal parsing, project submission, plan retrieval, approval, pause/resume/cancel, and per-task skip/retry operations.",
  "concepts": [
    "Deep Work API",
    "FastAPI router",
    "goal parsing",
    "project lifecycle",
    "background planning",
    "task retry",
    "task skip",
    "Pydantic validation",
    "project enrichment",
    "REST endpoints"
  ],
  "categories": [
    "Deep Work",
    "API"
  ],
  "source_docs": [
    "519f0114163f480a"
  ],
  "backlinks": null,
  "word_count": 487,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/deep_work/api.py` is the HTTP interface for PocketPaw's Deep Work orchestration system. It translates REST requests into calls to the `deep_work` package's async functions and returns JSON responses suitable for the dashboard's project management UI.

## Endpoint Design

### POST /parse-goal

Accepts a `ParseGoalRequest` (min 10, max 5000 characters) and runs `GoalParser.parse()`. Returns `{"success": true, "goal_analysis": {...}}` on success. This is a preview step — the UI can show the user what the AI inferred about their goal before committing to a full planning run.

Error handling distinguishes `RuntimeError` (returned as 502, indicating a downstream LLM failure) from generic exceptions (500). The 502 distinction matters for the client: a 502 suggests retrying later, while a 500 suggests a bug.

### POST /start

Accepts a `StartDeepWorkRequest` with `description`, `research_depth`, and an optional `goal_analysis` dict. The `goal_analysis` field allows the UI to pass the result of a prior `/parse-goal` call, skipping re-parsing and saving an LLM round-trip. Planning runs in a background task via `asyncio.create_task()` so the endpoint returns immediately with the project in `PLANNING` status.

### GET /projects/{id}/plan

Returns the project's plan with an `execution_levels` field that shows task dependencies. The frontend uses this to render the project's task graph.

### POST /projects/{id}/approve

Transitions the project to `EXECUTING`. If the project doesn't exist or is in a non-approvable state, returns 404 or 409.

### POST /projects/{id}/pause and /resume

Pause suspends task dispatch; resume restarts it. These are idempotent: pausing an already-paused project is a no-op.

### POST /projects/{id}/cancel

Added in Deep Work v2 (2026-02-26). Cancels the project and stops all running tasks. This is terminal — a cancelled project cannot be resumed.

### POST /projects/{id}/tasks/{tid}/skip

Marks a task as skipped, allowing the project to proceed past a stuck or unwanted task.

### POST /projects/{id}/tasks/{tid}/retry

Added in v2. Manually retries a failed task. Useful when a task failed due to a transient error (API rate limit, network timeout) that has since resolved.

## _enrich_project_dict()

This helper adds `folder_path` and `file_count` to project dicts before returning them to the client. The `folder_path` is the directory where the project's agent outputs are written; `file_count` is the number of non-hidden files in that directory. The frontend's output panel uses these to link directly to the project's files and show progress at a glance.

## Pydantic Request Models

`ParseGoalRequest` and `StartDeepWorkRequest` use Pydantic `Field` validators to enforce description length limits (10–5000 characters). This prevents empty or absurdly long inputs from reaching the LLM.

## Known Gaps

- The background planning task (`_plan_in_background`) is created with `asyncio.create_task()` but there is no reference kept to the task object. If the task raises an unhandled exception, it will be logged as a warning by Python's async infrastructure but won't surface to the client or stop the server — the project will be stuck in `PLANNING` state.
- There is no WebSocket event for planning failure; clients must poll `GET /projects/{id}/plan` to detect stuck planning runs.