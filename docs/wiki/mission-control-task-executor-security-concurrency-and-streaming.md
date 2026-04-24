---
{
  "title": "Mission Control Task Executor: Security, Concurrency, and Streaming",
  "summary": "Tests for MCTaskExecutor, the component that runs agent tasks in the background, streams results, and broadcasts events. Covers singleton lifecycle, UUID validation, error sanitization, duplicate-execution prevention, and background task completion.",
  "concepts": [
    "MCTaskExecutor",
    "task execution",
    "singleton",
    "UUID validation",
    "error sanitization",
    "rate limiting",
    "duplicate execution prevention",
    "background tasks",
    "event broadcasting",
    "AgentEvent",
    "streaming",
    "security"
  ],
  "categories": [
    "multi-agent",
    "security",
    "execution",
    "testing",
    "test"
  ],
  "source_docs": [
    "318b305041f94eed"
  ],
  "backlinks": null,
  "word_count": 585,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MCTaskExecutor` is the execution engine for Mission Control: it takes a task assignment, locates the target agent, builds a prompt, runs the agent, streams results back, and updates task status throughout. This test file is unusually security-conscious — it explicitly covers injection prevention, information disclosure, and resource exhaustion scenarios.

## Singleton Lifecycle

The executor is a module-level singleton accessed via `get_mc_task_executor()`. Tests confirm:
- `test_singleton_pattern`: Multiple calls to `get_mc_task_executor()` return the same instance, so running tasks are not lost across calls.
- `test_reset_executor`: `reset_mc_task_executor()` destroys the singleton, enabling clean test isolation. Without this reset, a task started in one test would appear as "running" in the next.

## Core Execution Flow

- `test_execute_invalid_task_id`: A non-UUID task ID must be rejected before any store lookup. This prevents format injection where a crafted ID could cause unexpected store behavior.
- `test_execute_task_not_found` / `test_execute_agent_not_found`: The executor must return clear errors when the task or agent does not exist, rather than raising an unhandled exception that crashes the background loop.
- `test_execute_task_success`: Happy path — a valid task and agent result in a completed task with `status=DONE`.
- `test_execute_task_updates_status`: Status transitions are persisted at each phase: `PENDING → IN_PROGRESS → DONE`. This enables real-time progress tracking in the dashboard.
- `test_execute_task_with_error`: When the agent raises, the task transitions to `BLOCKED` (not `DONE`). The test was updated to set `max_retries=0` because the default is 1 retry — without this, the test would need to trigger two consecutive failures.

## Concurrency Guards

- `test_duplicate_execution_prevented`: If `execute_task` is called for a task that is already running, the second call must be ignored. Without this guard, the same task could run twice simultaneously, producing duplicate outputs and double-billing the LLM.
- `test_is_task_running` / `test_get_running_tasks` / `test_stop_task_not_running`: The executor exposes introspection methods so the API can report which tasks are in flight and allow graceful cancellation.
- `test_background_execution_completes`: `execute_task_background` schedules execution asynchronously. This test was added to catch a self-defeating bug where the background coroutine was created but never awaited, so tasks appeared to start but never finished.

## Event Broadcasting

- `test_broadcasts_events`: As the agent produces output, the executor broadcasts `AgentEvent` objects on the message bus. Downstream subscribers (websocket clients, logs) depend on receiving every event — this test captures events in a listener and verifies they all arrive.

## Prompt Construction

- `test_build_task_prompt`: The executor assembles the prompt from the task description, agent persona, and any attached context. The test verifies the prompt includes expected sections, since a missing section could cause the agent to respond in the wrong persona or miss its instructions.

## Security Features (`TestSecurityFeatures`)

- **UUID validation**: Task IDs must be valid UUIDs. This prevents a caller from passing a string like `../secrets` as a task ID, which could cause path traversal in a file-based store.
- **Error message sanitization**: When an agent fails, the error returned to the caller must not include raw stack traces or internal paths, preventing information disclosure to untrusted callers.
- **Rate limiting**: The executor enforces a rate limit on task submissions to prevent a rogue caller from flooding the system with tasks and exhausting LLM quota.

## Known Gaps

The comment history in the file header documents two known evolution points: the `max_retries` default changed from 0 to 1 between sprints, requiring a test update; and `execute_task_background` had a self-defeating bug that was caught and fixed. No open TODOs remain, but the rate-limiting tests are present as stubs with basic assertions — the specific rate values are not yet pinned.
