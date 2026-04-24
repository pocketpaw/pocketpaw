---
{
  "title": "Deep Work Task Model Extensions: New Field Tests",
  "summary": "This suite tests the Deep Work extensions added to the core Task model in PocketPaw's mission control system, specifically the five new fields—project_id, task_type, blocks, active_description, and estimated_minutes—that enable multi-task project management. It verifies defaults, settability, serialization, backward compatibility, and round-trip fidelity.",
  "concepts": [
    "Task_model",
    "project_id",
    "task_type",
    "blocks",
    "active_description",
    "estimated_minutes",
    "backward_compatibility",
    "round_trip",
    "to_dict",
    "from_dict",
    "mutable_default",
    "Deep_Work",
    "mission_control_models"
  ],
  "categories": [
    "testing",
    "deep-work",
    "data-models",
    "serialization",
    "test"
  ],
  "source_docs": [
    "8077534bca849292"
  ],
  "backlinks": null,
  "word_count": 530,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_models.py` tests additions made to the `Task` model in `pocketpaw.mission_control.models` when the Deep Work feature was introduced. Before Deep Work, tasks were standalone units; the new fields link tasks to projects, encode dependency graphs, and carry scheduling metadata.

## Why These Fields Exist

The Deep Work system introduces multi-task projects where tasks have explicit dependencies on one another. The five new fields serve distinct purposes:

- **project_id**: Associates a task with its parent `Project`. Without this, the scheduler cannot group tasks by project or detect project-wide completion.
- **task_type**: Distinguishes `agent` tasks (run by AI) from `human` tasks (require human action) and `review` tasks. The executor and human router use this field to route tasks to the correct handler.
- **blocks**: A list of task IDs that this task blocks — i.e., tasks that cannot start until this one completes. This is the "forward edge" in the dependency graph. The inverse (`blocked_by`) lives on the blocked task itself.
- **active_description**: A mutable description that the executor updates as the task progresses. Useful for real-time status in the dashboard.
- **estimated_minutes**: Time estimate for scheduler load balancing and plan-ready notifications.

## Default Values

`test_new_field_defaults` confirms that a bare `Task()` gets safe defaults: `project_id=None`, `task_type="agent"` (AI-first assumption), `blocks=[]` (no forward edges by default), `active_description=""`, and `estimated_minutes=None` (optional).

The `blocks=[]` default is notable: it uses the standard Python mutable-default-argument pattern through Pydantic/dataclass field defaults, ensuring that each Task instance has its own independent list rather than sharing a class-level list.

## Settability

`test_new_fields_settable` confirms all five fields can be set via the constructor, covering the normal project-creation path where the planner populates every field from its LLM-generated plan.

## Serialization

`test_to_dict_includes_new_fields` verifies all five fields appear in the `to_dict()` output. `test_to_dict_new_fields_defaults` checks that defaults serialize correctly (e.g., `None` values are included rather than omitted, which matters for consumers that iterate over all keys).

These tests exist because `to_dict()` is the serialization contract used when writing tasks to the file store. Missing a field means data loss on restart.

## from_dict() and Backward Compatibility

`test_from_dict_with_new_fields` verifies that a dict containing the new fields is correctly deserialized. `test_from_dict_backward_compat` is the defensive test: a dict without any of the new fields (representing a task saved before the Deep Work feature was added) should still load correctly, with new fields defaulting gracefully. Without this test, a store migration failure would corrupt existing user data on upgrade.

## Round-Trip

`test_round_trip` confirms that `Task` → `to_dict()` → `from_dict()` → `Task` produces an identical object for a fully-populated task. This is the correctness contract for persistence.

## blocks List Independence

`test_blocks_list_is_independent` verifies that two separately constructed Task objects do not share the same `blocks` list instance. This is a classic Python mutable default argument pitfall — if the default `[]` were shared at the class level, appending to one task's blocks would corrupt all other tasks' blocks lists.

## Known Gaps

No tests cover the `task_type` enum values or validation — invalid strings (e.g., `task_type="robot"`) are not tested for rejection or fallback behavior. The relationship between `blocks` (forward edges) and `blocked_by` (reverse edges) is not tested for consistency here; that logic lives in the session and scheduler tests.
