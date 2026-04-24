---
{
  "title": "Mission Control Manager: High-Level Orchestration Layer",
  "summary": "MissionControlManager sits above the raw file store and combines storage operations with business logic: creating tasks with automatic activity logging, resolving @mention notifications, managing agent heartbeats, and orchestrating project lifecycle including on-disk directory creation.",
  "concepts": [
    "MissionControlManager",
    "activity logging",
    "mention extraction",
    "notification",
    "project directory",
    "task lifecycle",
    "heartbeat",
    "standup",
    "singleton",
    "Deep Work"
  ],
  "categories": [
    "mission-control",
    "agent-orchestration",
    "project-management"
  ],
  "source_docs": [
    "aab312359e929e56"
  ],
  "backlinks": null,
  "word_count": 431,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MissionControlManager` is the primary facade for all Mission Control operations. While `FileMissionControlStore` provides raw CRUD over JSON files, the manager adds business logic: every task creation logs an activity, every @mention fires a notification, and every project gets a real directory on disk.

This pattern deliberately mirrors `MemoryManager` in the memory subsystem — keeping storage logic separate from business logic allows swapping the storage backend without touching business rules.

## @Mention Extraction

Messages posted to tasks are scanned for `@name` patterns using a compiled regex:

```python
MENTION_PATTERN = re.compile(r"@(\w+)", re.IGNORECASE)
```

`_extract_mentions()` returns the list of mentioned names. `_notify_mentions()` resolves each name to an agent ID (case-insensitive lookup), then creates a `Notification` record. The two-step approach decouples message posting from notification delivery — notifications are queried and delivered asynchronously by the heartbeat daemon rather than blocking the post operation.

## Activity Feed

Every mutating operation calls `_log_activity()`, appending an `Activity` record to the audit trail. Without automatic logging, the activity feed would require explicit calls at every call site — a historically frequent source of missing audit entries.

## Project Directory Management

When a project is created, `create_project()` not only persists the project record but also creates `~/pocketpaw-projects/{id}/` on disk. This matters because Deep Work projects have associated working files that agents write during task execution. If the directory does not exist, agent writes fail with obscure `FileNotFoundError` exceptions.

```python
async def create_project(self, ...) -> Project:
    ...
    project_dir = get_project_dir(project.id)
    project_dir.mkdir(parents=True, exist_ok=True)
```

`delete_project()` uses `shutil.rmtree()` to remove the directory. The `ensure_project_directories()` migration method handles projects created before this feature landed by creating missing directories for all existing projects on startup.

## Agent Heartbeat Recording

`record_heartbeat()` is called by the `HeartbeatDaemon` after each agent wakeup. It updates the `last_heartbeat` timestamp and logs a minimal heartbeat activity (no content, just "agent checked in") to avoid flooding the feed with noise.

## Daily Standup Generation

`generate_standup()` produces a human-readable summary of the current Mission Control state: active agents, open tasks, recent activity. Used to give users a quick status snapshot without querying multiple endpoints.

## Known Gaps

- **`get_project_progress()` skipped count**: The changelog mentions a `skipped` count was added to `get_project_progress()`, but the method is not present in the AST extraction — it may live in the Deep Work manager or be yet to be implemented.
- **No pagination in list methods**: All `list_*` methods pass filters to the store but do not expose pagination cursors. At high record counts this will be slow.
- **Standup format is plain text**: `generate_standup()` returns a string with no structured format for dashboard widgets.