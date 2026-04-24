---
{
  "title": "Task Skip Semantics: Dependency Unblocking and Project Completion",
  "summary": "Covers the test suite that validates how SKIPPED tasks interact with the dependency scheduler and project completion logic in PocketPaw's deep-work system. Ensures skipped tasks satisfy blocked_by conditions, contribute to project completion, and appear correctly in progress reporting.",
  "concepts": [
    "TaskStatus.SKIPPED",
    "DependencyScheduler",
    "blocked_by",
    "MissionControlManager",
    "project completion",
    "progress reporting",
    "FileMissionControlStore",
    "deep-work",
    "task dependencies"
  ],
  "categories": [
    "testing",
    "task scheduling",
    "deep-work",
    "test"
  ],
  "source_docs": [
    "00742ee34db78ccd"
  ],
  "backlinks": null,
  "word_count": 484,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's deep-work system models complex projects as directed acyclic graphs of tasks with dependency edges. A key design question is: if a blocking task is skipped rather than completed, should its dependents remain blocked forever? This test file answers that question and pins the semantics in code.

## Why SKIPPED Unblocks Dependents

The `TestSkippedUnblocksDependents` class exists because the naive implementation of `blocked_by` only checked for `DONE` status. When a user or the system chooses to skip a task — because it's irrelevant, superseded, or deferred — any task waiting on it would be stuck indefinitely. That breaks the entire scheduling model. The tests confirm three distinct scenarios:

- **Pure skip**: A single SKIPPED blocker unblocks its dependent immediately.
- **Mixed done+skip**: A task blocked by two tasks, one DONE and one SKIPPED, becomes runnable.
- **Partial skip with pending**: If one blocker is SKIPPED but another is still PENDING, the dependent remains blocked.

The third case is the defensive edge case. Without it, a careless "treat SKIPPED like DONE" implementation might unblock tasks that still have genuine pending dependencies, corrupting the execution order.

## Project Completion with Skipped Tasks

`TestSkippedProjectCompletion` validates that a project whose all tasks are SKIPPED is marked COMPLETED rather than left in perpetual limbo. This prevents ghost projects from accumulating in the system — projects that are technically finished but have no DONE tasks. Two variants are tested:

- All tasks SKIPPED → project completes.
- Mix of DONE and SKIPPED tasks → project completes.

Without this, any project with even one skipped task would never auto-close, polluting the active project list and triggering misleading in-progress indicators.

## Progress Reporting

`TestProgressWithSkipped` uses a real `MissionControlManager` (not a mock) to verify that skipped tasks are counted in `progress.skipped`, contribute to the completion percentage, and do not appear in the pending queue for human assignment. This matters for dashboards: if skipped tasks were invisible to progress accounting, the percentage would never reach 100% for projects with skipped items.

## Enum Correctness

`TestSkippedEnum` is the simplest class but arguably the most foundational — it pins the wire format of `TaskStatus.SKIPPED`. The value `'skipped'` must survive JSON serialization and deserialization unchanged. If the enum value drifted (e.g., to `'skip'` or `'SKIPPED'`), stored task data would fail to deserialize, silently defaulting to an unknown state.

## Fixture Design

The `mock_manager` fixture wires together three mocked collaborators — `MCTaskExecutor`, `HumanTaskRouter`, and `MissionControlManager` — so the `DependencyScheduler` can run in isolation. The `real_manager` fixture uses a temp directory and a live `FileMissionControlStore` for the progress tests, which require actual persistence semantics.

## Known Gaps

None identified in this test file. The coverage of the three skip-interaction scenarios (pure, mixed, partial) appears intentionally complete.

## Usage

```python
# Example: checking that a skipped task satisfies blocked_by
task_a = Task(id="a", status=TaskStatus.SKIPPED)
task_b = Task(id="b", blocked_by=["a"])
# scheduler should now consider task_b runnable
assert scheduler.is_runnable(task_b, all_tasks={"a": task_a})
```
