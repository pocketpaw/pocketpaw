---
{
  "title": "Mission Control Data Models",
  "summary": "This module defines all core dataclasses and enums for Mission Control covering agent profiles, tasks, messages, activities, documents, and notifications. The models use Python dataclasses with ISO 8601 timestamps and StrEnum statuses, and have grown through two major revisions to support Deep Work v2 autonomous execution fields.",
  "concepts": [
    "AgentProfile",
    "Task",
    "TaskStatus",
    "TaskPriority",
    "AgentStatus",
    "Message",
    "Activity",
    "Document",
    "Notification",
    "StrEnum",
    "dataclass",
    "Deep Work",
    "SKIPPED status"
  ],
  "categories": [
    "mission-control",
    "data-models",
    "agent-orchestration"
  ],
  "source_docs": [
    "dec319c058e05162"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`models.py` is the canonical type layer for Mission Control. Every entity flowing through the manager, store, and heartbeat daemon is defined here. The design follows PocketPaw's convention of using Python `dataclasses` (not Pydantic) for simplicity and minimal dependencies, with `StrEnum` for all status fields.

## Design Choices

**Dataclasses over Pydantic**: Pydantic adds validation overhead and a heavier import chain. Since Mission Control is primarily internal state, the lightweight dataclass approach is preferred.

**ISO 8601 string timestamps**: All timestamps are stored as `str` rather than `datetime` objects. This avoids timezone-aware/naive confusion during JSON serialization. The `now_iso()` helper always produces UTC timestamps.

**StrEnum for statuses**: `StrEnum` values serialize to their string representation automatically, avoiding the `.value` call that plain `Enum` requires — a frequent source of bugs.

## Entity Breakdown

### AgentProfile
Represents a registered AI agent with:
- `session_key`: maps to a PocketPaw session for execution routing
- `backend`: selects which agent runtime handles execution (e.g., `claude_agent_sdk`)
- `level` (`AgentLevel`): controls autonomy — `INTERN` requires approval, `LEAD` has full autonomy
- `last_heartbeat`: set by the heartbeat daemon to track liveness

### Task
The most complex entity with two generations of fields. Original fields cover the basic lifecycle (`status`, `priority`, `assignee_ids`, `blocked_by`). Deep Work fields (2026-02-12) add project grouping (`project_id`), typed task kinds (`task_type`: agent/human/review), and dependency tracking (`blocks`). Deep Work v2 fields (2026-02-26) support autonomous execution:

```python
output: str | None = None          # execution result stored on task
retry_count: int = 0               # current retry attempt
max_retries: int = 1               # auto-retry cap
timeout_minutes: int | None = None # per-task wall-clock limit
error_message: str | None = None   # last failure reason
```

The `SKIPPED` status (added 2026-02-12) exists because Deep Work pipelines may encounter tasks that are no longer relevant mid-run. Marking them `DONE` would be misleading; `BLOCKED` would be semantically incorrect.

### Message
Represents a comment on a task. `mentions` is a pre-extracted list stored alongside raw `content`. Pre-extraction at write time means the heartbeat daemon never needs to regex-scan message content on each cycle.

### Activity
The audit trail entity. Each mutating operation appends an `Activity` with a typed `ActivityType` that feeds the dashboard's real-time event stream.

### Document
Represents agent work products. The `version` field auto-increments in the store on update, providing lightweight change history without a full versioning system.

### Notification
Tracks @mentions and alerts directed at specific agents. The two-state model (`delivered` / `read`) mirrors email semantics: delivered means the notification was sent to the agent's inbox; read means the agent acknowledged it.

## Known Gaps

- **No schema migration**: The `from_dict()` methods use `data.get(field, default)` for all fields, so old records missing Deep Work v2 fields silently get default values. There is no explicit migration step or schema version field.
- **`metadata` is untyped**: All entities carry `metadata: dict[str, Any]` for extensibility but with no schema or validation.