---
{
  "title": "MCTaskExecutor — Secure, Streaming AI Agent Task Execution",
  "summary": "`MCTaskExecutor` runs AI agents against Mission Control tasks with real-time WebSocket streaming of output, auto-saves deliverable documents, retries failed tasks up to a configurable limit, enforces per-task timeouts, and limits concurrent executions with UUID validation and error sanitisation as security controls.",
  "concepts": [
    "MCTaskExecutor",
    "AgentRouter",
    "asyncio.create_task",
    "task retry",
    "asyncio.wait_for",
    "timeout",
    "UUID validation",
    "error sanitisation",
    "concurrent task limit",
    "WebSocket events",
    "deliverable document",
    "stop_all_project_tasks"
  ],
  "categories": [
    "Multi-Agent Orchestration",
    "Agent Execution"
  ],
  "source_docs": [
    "f2ed02b6728b36fd"
  ],
  "backlinks": null,
  "word_count": 400,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MCTaskExecutor` was created on 2026-02-05 and significantly enhanced on 2026-02-26 with retry logic, timeouts, output storage on `Task.output`, and `stop_all_project_tasks`. It bridges Mission Control's task management with PocketPaw's agent execution layer.

## Isolation via AgentRouter

Each task execution creates a dedicated `AgentRouter` instance. This provides:

1. **Backend isolation** — the agent's `backend` field (`claude_agent_sdk`, `pocketpaw_native`, `open_interpreter`) determines which LLM backend is used
2. **Context isolation** — each task gets its own conversation context; agents cannot accidentally share history

## Background Execution Pattern

```python
async def execute_task_background(task_id: str, agent_id: str) -> bool:
    task = asyncio.create_task(self._run_task_with_retry(...))
    self._running_tasks[task_id] = task
```

Tasks run as `asyncio.Task` objects stored in `_running_tasks`. This enables `stop_task` to call `task.cancel()` cooperatively. The bug fixed on 2026-02-12 was a case where the background task was immediately awaited, making `execute_task_background` blocking rather than non-blocking.

## Retry Logic

```python
for attempt in range(max_retries + 1):
    try:
        result = await asyncio.wait_for(
            self._stream_task(router, prompt, task_id, chunks),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        ...  # emit mc_task_completed with status "timeout"
    except Exception:
        ...  # retry if attempt < max_retries, else fail
```

`asyncio.wait_for` enforces `timeout_minutes` per task. Retry broadcasts `mc_task_retry` events so the dashboard can show retry status to the user.

## Security Controls

| Control | Mechanism |
|---------|----------|
| Max concurrent tasks | `MAX_CONCURRENT_TASKS = 5`; new tasks rejected beyond limit |
| UUID validation | `_is_valid_uuid` checked before any store lookup |
| Error sanitisation | `_sanitize_error` strips stack traces; max `MAX_ERROR_MESSAGE_LENGTH` chars |
| Audit logging | Security events logged at `WARNING` level with task/agent context |

UUID validation prevents path-traversal-style attacks where a crafted `task_id` might escape expected storage paths. Error sanitisation prevents internal details (file paths, DB queries) from leaking to the agent's output stream.

## Output Persistence

On successful completion, `_save_task_deliverable` saves the agent's full output as a `Document` in MC, linked to the task. It also writes to `Task.output` for cross-task chaining: a subsequent task can reference `previous_task.output` in its prompt.

## Project-Wide Stop

`stop_all_project_tasks(project_id)` cancels all running tasks associated with a project. This supports the Deep Work 'pause project' operation, which needs to halt all in-flight agent work atomically.

## Known Gaps

Retry logic uses a simple loop with no exponential backoff; consecutive retries happen immediately, which could hammer an API that returned a rate-limit error. `_stream_task` accumulates all output in `output_chunks` (in-memory list); for very long-running tasks producing large outputs, this could exhaust memory.