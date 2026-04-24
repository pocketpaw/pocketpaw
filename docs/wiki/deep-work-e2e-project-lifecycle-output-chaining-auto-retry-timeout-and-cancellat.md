---
{
  "title": "Deep Work E2E: Project Lifecycle, Output Chaining, Auto-Retry, Timeout, and Cancellation",
  "summary": "This test suite exercises PocketPaw's Deep Work (multi-agent task orchestration) system end-to-end through the FastAPI test client, covering project approval, task dependency cascades, output persistence, auto-retry on failure, timeout triggers, project cancellation in all lifecycle states, and the manual retry and skip APIs.",
  "concepts": [
    "Deep Work",
    "task DAG",
    "project lifecycle",
    "auto-retry",
    "task timeout",
    "BLOCKED state",
    "output chaining",
    "dependency cascade",
    "project cancellation",
    "manual retry"
  ],
  "categories": [
    "testing",
    "deep work",
    "task orchestration",
    "end-to-end",
    "test"
  ],
  "source_docs": [
    "1c22c97e1dcbb235"
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

Deep Work is PocketPaw's multi-agent orchestration layer. Projects contain DAGs of tasks with dependencies; when a task completes its output is chained to dependent tasks. This file tests the full lifecycle from project approval through completion, plus every failure and cancellation path.

## Test Infrastructure

The suite uses a real `AsyncClient` bound to the FastAPI app with `httpx`. The `session`, `manager`, and `executor` fixtures provide an in-process deep work session manager and executor, with agent execution mocked out so tests can control what each agent "produces" without invoking a real LLM.

## Project Approval and Task Dispatch

`test_approve_dispatches_ready_tasks` verifies the core invariant: approving a project transitions all tasks with no pending dependencies to `READY` state and enqueues them for execution. Without this, no work would ever start after a project is created and planned.

## Dependency Cascade

`test_task_completion_cascades_to_dependents` verifies that when task A completes, any task B that had A as its only unsatisfied dependency transitions to `READY`. This is the core of the DAG execution engine — missing this cascade causes projects to stall even when all prerequisites are satisfied.

## Output Chaining

`TestOutputChaining.test_output_stored_after_successful_execution` verifies that `task.output` is populated after the executor completes the task. Output is how downstream tasks receive the results they depend on — if output is not persisted, the cascade works but dependent tasks have no data to work with.

## Auto-Retry

`TestAutoRetry` covers two retry scenarios:

1. `test_retry_fires_then_succeeds` — The first execution attempt streams a failure, the retry mechanism fires, the second attempt succeeds. The test asserts the task ends in `COMPLETED` state.
2. `test_retries_exhausted_goes_blocked` — All retry attempts fail. The task transitions to `BLOCKED` state rather than staying in an infinite retry loop or crashing.

The `BLOCKED` state is important for operator visibility: it surfaces the stuck task in the UI so a human can intervene (via the manual retry API) rather than hiding the failure.

## Task Timeout

`TestTaskTimeout.test_timeout_triggers_retry_or_blocked` verifies that tasks with `timeout_minutes` set do not run forever. When the timeout fires, the task either retries (if retries remain) or goes `BLOCKED`. This prevents a single stuck agent from holding up the entire project indefinitely.

## Project Cancellation

`TestProjectCancellation` covers cancellation from every lifecycle state:
- Cancelling an executing project stops running tasks.
- Cancelling a paused project succeeds.
- Cancelling a completed project fails (idempotency protection).
- Cancelling an already-cancelled project fails (no double-cancel side effects).
- Cancelling a nonexistent project returns 404.

## Manual Retry and Skip APIs

`TestManualRetryAPI` tests the operator escape hatch: `POST /projects/{id}/tasks/{tid}/retry` on a `BLOCKED` task reschedules it. The tests also verify that retrying a non-blocked task fails (preventing duplicate execution) and that cross-project retry attempts are rejected.

`TestSkipTaskAPI` tests the `skip` endpoint, which marks a blocked task as skipped and unblocks its dependents — useful when a task is stuck on an external dependency that will never resolve.

## Known Gaps

There is no test for partial project completion followed by server restart and recovery — whether the executor correctly resumes in-flight tasks after a crash is not covered here.