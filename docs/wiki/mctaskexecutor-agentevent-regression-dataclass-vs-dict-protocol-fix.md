---
{
  "title": "MCTaskExecutor AgentEvent Regression: Dataclass vs Dict Protocol Fix",
  "summary": "This regression test reproduces a bug from PR #482 where MCTaskExecutor called .get() on AgentEvent dataclass objects, causing AttributeError crashes. The test covers the full executor pipeline — event streaming, task status transitions, output capture, and error handling — using a fake agent that emits AgentEvent dataclass objects rather than plain dicts.",
  "concepts": [
    "MCTaskExecutor",
    "AgentEvent",
    "dataclass_vs_dict",
    "AttributeError_regression",
    "PR_482",
    "claude_agent_sdk",
    "event_streaming",
    "task_status_transitions",
    "output_capture",
    "error_handling",
    "reset_mc_task_executor",
    "fake_router",
    "AgentStatus"
  ],
  "categories": [
    "testing",
    "regression",
    "executor",
    "agent-protocol",
    "bug-fix",
    "test"
  ],
  "source_docs": [
    "2837e77e792d4428"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_executor_agent_event.py` is a targeted regression test for a specific bug fixed in PR #482. The `MCTaskExecutor` was treating `AgentEvent` objects as dictionaries (calling `.get('type')`, `.get('content')`) when they are actually Python dataclasses with typed attributes (`.type`, `.content`, `.metadata`). This caused `AttributeError: 'AgentEvent' object has no attribute 'get'` in production.

## Why This Module Exists

The bug was subtle because PocketPaw supports multiple agent backends, and some backends emitted plain dicts while others emitted `AgentEvent` dataclasses. The executor code path that was broken only triggered when using the `claude_agent_sdk` backend, which was not the primary test target at the time of initial development.

## The Bug

```python
# Broken code (pre-fix):
event_type = event.get('type')  # Works for dict, crashes for AgentEvent dataclass

# Correct code (post-fix):
event_type = event.type  # Works for AgentEvent dataclass
```

The test suite documents the exact error so that future contributors understand why the attribute-access pattern must be used rather than dict-style `.get()`.

## Test Infrastructure

### Fake Agent and Task Objects

`_make_fake_agent()` creates a `MagicMock` with all agent attributes set: `id`, `name`, `role`, `specialties`, `backend="claude_agent_sdk"`, and `status=AgentStatus.IDLE`. The `backend` attribute is significant — it selects the Claude Agent SDK code path that emits `AgentEvent` dataclasses.

`_make_fake_task()` creates a task mock with all fields needed by the executor: timeout, retry configuration, output, and error message fields (the v2 additions).

### Fake Router

`_fake_router_run(prompt)` is an async generator that yields `AgentEvent` dataclass instances rather than dicts:

```python
async def _fake_router_run(prompt):
    yield AgentEvent(type="content", content="Task output here", metadata={})
    yield AgentEvent(type="done", content="", metadata={})
```

This is the exact pattern that the `claude_agent_sdk` backend uses — and the pattern that exposed the bug.

## What the Tests Cover

The regression test verifies the full happy path:

1. Executor receives a task assigned to a `claude_agent_sdk` agent.
2. The router yields `AgentEvent` dataclass objects during streaming.
3. The executor correctly reads `.type` and `.content` attributes (not `.get()`).
4. Task `output` is populated from the content event.
5. Task status transitions correctly: INBOX → IN_PROGRESS → DONE.

Error path tests verify that `AgentEvent(type="error", content="LLM error")` objects are correctly handled — the error message is captured in `task.error_message` and the task transitions to FAILED.

## reset_mc_task_executor

The test calls `reset_mc_task_executor()` in setup to ensure a fresh executor instance. The executor is a singleton in production (to maintain running task state), so tests must explicitly reset it to avoid cross-test pollution.

## Known Gaps

The test uses synchronous `MagicMock` for the manager rather than a real store. If the executor's interaction with the manager changes (e.g., adding optimistic locking), the mocks would not catch it. Integration tests with a real store are left to the Deep Work v2 suite.
