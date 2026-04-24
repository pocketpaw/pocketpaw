---
{
  "title": "Deep Work Data Models — Project, TaskSpec, AgentSpec, and PlannerResult",
  "summary": "This module defines the core data structures for the Deep Work orchestration layer: Project (top-level orchestration unit), TaskSpec and AgentSpec (planning-phase blueprints), and PlannerResult (complete planner output). These models separate planning artifacts from execution artifacts, allowing the planner to produce a full plan before any Mission Control objects are created.",
  "concepts": [
    "Project",
    "ProjectStatus",
    "TaskSpec",
    "AgentSpec",
    "PlannerResult",
    "Deep Work models",
    "planning layer",
    "execution layer",
    "materialization",
    "StrEnum",
    "Mission Control"
  ],
  "categories": [
    "deep-work",
    "data-models",
    "planning",
    "orchestration"
  ],
  "source_docs": [
    "d05dd7600c9a3889"
  ],
  "backlinks": null,
  "word_count": 505,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The Deep Work system has two distinct object layers: the **planning layer** (these models) and the **execution layer** (Mission Control Tasks, Agents, Documents). The separation exists because planning is exploratory — the planner produces a speculative plan that the user approves before it becomes real. Materializing MC objects during planning would create orphaned tasks if the user cancels or replans.

## ProjectStatus Enum

`ProjectStatus` defines the full project lifecycle as a `StrEnum`:

```python
class ProjectStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

`CANCELLED` was added in Deep Work v2 (2026-02-26) to support mid-execution project termination. Before that addition, there was no way to represent a project that was deliberately stopped — projects either completed, failed, or sat in `PAUSED` indefinitely.

`AWAITING_APPROVAL` is a holding status after planning completes but before the user approves. This prevents automatic execution of plans the user hasn't reviewed.

## Project Dataclass

`Project` is the top-level orchestration unit. It holds references (IDs) to MC objects rather than the objects themselves, keeping it lightweight and JSON-serializable.

Key fields:
- **`planner_agent_id`** — tracks which agent produced the plan, enabling audit
- **`prd_document_id`** — links to the PRD stored as an MC Document
- **`task_ids`** — list of MC Task IDs; populated during materialization, not planning
- **`metadata`** — extensible dict for future extensions without schema changes
- **`creator_id`** — defaults to `"human"` but can be an agent ID for agent-initiated projects

## TaskSpec Dataclass

`TaskSpec` is a lightweight task blueprint produced by the planner. It is *not* a Mission Control Task — it doesn't have an agent assigned, no status tracking, no message history. It's a specification that gets materialized into a real Task when the user approves the plan.

Key fields added in Deep Work v2:
- **`max_retries`** — copied to the real Task on materialization, governing how many times the executor retries on failure
- **`timeout_minutes`** — copied to the real Task, bounding execution time
- **`blocked_by_keys`** — dependency references using planner-assigned string keys (not MC IDs yet, since tasks don't exist yet)

## AgentSpec Dataclass

`AgentSpec` represents a recommended team member from the planner. It describes what role and capabilities are needed, not a specific existing agent. The session layer uses `AgentSpec` to find or create suitable agents during materialization.

## PlannerResult Dataclass

`PlannerResult` bundles everything the planner produces:
- **`prd_content`** — the full PRD markdown
- **`tasks`** — list of `TaskSpec` objects
- **`recommended_agents`** — list of `AgentSpec` objects
- **`research_notes`** — raw research output (stored as a document for reference)

This single object is passed from `PlannerAgent.plan()` to `DeepWorkSession._materialize_tasks()`, making the handoff clean and testable.

## Known Gaps

- `Project.from_dict()` and the other `from_dict` methods don't validate that referenced IDs actually exist in Mission Control — stale references (from deleted tasks or agents) will deserialize without error.
- There is no versioning field on Project or PlannerResult, making schema migration difficult if field shapes change.
