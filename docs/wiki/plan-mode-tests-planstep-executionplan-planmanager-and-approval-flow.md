---
{
  "title": "Plan Mode Tests: PlanStep, ExecutionPlan, PlanManager, and Approval Flow",
  "summary": "PocketPaw's plan mode allows agents to propose multi-step execution plans and wait for human approval before running potentially destructive operations. These tests cover `PlanStep` previews, `ExecutionPlan` assembly, `PlanManager` lifecycle (create, approve, reject, expire), and the async approval-wait mechanism.",
  "concepts": [
    "plan mode",
    "PlanStep",
    "ExecutionPlan",
    "PlanManager",
    "human-in-the-loop",
    "plan approval",
    "plan rejection",
    "TTL expiry",
    "asyncio event",
    "singleton manager"
  ],
  "categories": [
    "testing",
    "plan mode",
    "agent safety",
    "test"
  ],
  "source_docs": [
    "3424f53f88e066a5"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Plan mode is PocketPaw's human-in-the-loop safety mechanism. When an agent plans to run shell commands, write files, or make edits, it can first construct a plan, display it for review, and wait for explicit approval. This prevents autonomous agents from taking irreversible actions without oversight.

## PlanStep Previews

`TestPlanStep` verifies that each tool type renders a human-readable preview:

- **shell/bash**: shows the command string.
- **write_file**: shows the target file path.
- **edit**: shows file path and edit description.
- **generic tool**: falls back to a sensible default format.

Previews exist because the approval UI must show the user what will happen before they approve. A plan step that renders as `<PlanStep object at 0x...>` is useless for human review.

## ExecutionPlan Assembly

`TestExecutionPlan` validates the plan container:

- **`add_step()`**: appends steps in order.
- **`to_preview()`**: renders the full plan as a numbered list of step previews.
- **Empty preview**: an empty plan renders as a message indicating no steps, not as an empty string.
- **`to_dict()`**: serializes the plan for storage or transmission.

The empty plan case prevents the UI from displaying a blank modal — a confusing UX failure that would occur if `to_preview()` returned `""`.

## PlanManager Lifecycle

`TestPlanManager` covers the state machine for active plans:

- **`create_plan()`**: creates and stores a plan by ID.
- **`add_step_creates_plan()`**: a step can be added to a non-existent plan, which creates it implicitly — supporting incremental plan construction.
- **`approve_plan()`**: marks the plan approved and unblocks any waiting coroutine.
- **`reject_plan()`**: marks the plan rejected.
- **Non-existent plan approval/rejection**: returns a safe error value rather than raising `KeyError`.
- **`get_active_plan_expired()`**: plans have a TTL; an expired plan is not returned as active.
- **`clear_plan()`**: removes the plan from the manager.

Plan expiry is important for production use: if a user receives an approval request but never responds, the agent must not block indefinitely.

## Async Approval Wait

The async tests (`test_wait_for_approval`, `test_wait_for_rejection`, `test_wait_timeout`) are module-level async functions rather than class methods, testing the critical flow:

```python
async def test_wait_for_approval(manager):
    async def approve_later():
        await asyncio.sleep(0.05)
        manager.approve_plan(plan_id)
    asyncio.create_task(approve_later())
    result = await manager.wait_for_approval(plan_id)
    assert result is True
```

`approve_later()` simulates a user clicking "Approve" in the UI after a short delay. `wait_for_approval()` blocks until the event fires or times out. The timeout test verifies that a non-responsive user eventually unblocks the agent with a rejected/timed-out result — preventing deadlock.

## Singleton

`test_singleton()` verifies the module-level `PlanManager` instance is shared across imports — all agent components coordinate through the same manager without explicit dependency injection.

## Known Gaps

- No test for concurrent plans (multiple pending approvals at once).
- The TTL expiry mechanism is tested via `get_active_plan_expired` but the actual TTL value is not asserted.
- No test for serializing/deserializing plans across process restarts (persistence).
- The approval event uses asyncio internals — no test verifies behavior under a different event loop (e.g., uvloop).