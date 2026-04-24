---
{
  "title": "Mission Control: Data Models, File Store, and Manager Operations",
  "summary": "Tests for PocketPaw's multi-agent orchestration layer, covering AgentProfile and Task data models, the file-based persistence store, and the high-level MissionControlManager that coordinates agents, tasks, messages, documents, and heartbeats.",
  "concepts": [
    "Mission Control",
    "AgentProfile",
    "Task",
    "MissionControlManager",
    "FileMissionControlStore",
    "multi-agent orchestration",
    "heartbeat",
    "notifications",
    "activity feed",
    "standup",
    "singleton reset",
    "task status"
  ],
  "categories": [
    "multi-agent",
    "orchestration",
    "testing",
    "persistence",
    "test"
  ],
  "source_docs": [
    "3b3e3669a965397f"
  ],
  "backlinks": null,
  "word_count": 597,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Mission Control is PocketPaw's coordination layer for multi-agent systems. It tracks which agents exist, what tasks they have been assigned, what messages have been exchanged, and what documents have been produced. The `test_mission_control.py` suite validates the full stack from raw data models through file persistence to the high-level manager.

## Data Models (`TestModels`)

The models — `AgentProfile`, `Task`, `Message`, `Activity`, and `Notification` — are the lingua franca between all Mission Control components. Tests verify:

- **Defaults**: `AgentProfile` and `Task` have sensible defaults (e.g., `status=IDLE`, `priority=MEDIUM`) so callers can create minimal objects without specifying every field.
- **Round-trip serialization** (`test_agent_profile_to_dict`, `test_agent_profile_from_dict`, `test_task_to_dict`): Models serialize to plain dicts and deserialize back without data loss. This matters because the file store writes JSON and the API serializes over HTTP — any field dropped in round-trip becomes invisible.
- **Mentions** (`test_message_with_mentions`): Messages support `@mentions` of other agent IDs. The test confirms the mentions list is parsed and stored correctly, enabling notification routing.
- **Activity types** (`test_activity_types`): The `ActivityType` enum covers all expected event categories, which the activity feed depends on for filtering.

## File Store (`TestFileMissionControlStore`)

`FileMissionControlStore` persists all Mission Control data to JSON files in a configurable directory. Key tests:

- `test_save_and_get_agent` / `test_get_agent_by_name`: Agents can be looked up by ID or by name. Name-based lookup is used by human operators; ID lookup is used by agent-to-agent calls.
- `test_list_agents_filtered`: The store supports filtering agents by status (e.g., show only `ACTIVE` agents). Without this, the heartbeat daemon would wake every registered agent, including decommissioned ones.
- `test_save_and_get_task` / `test_list_tasks_by_status`: Tasks are retrievable by status to support work queues (e.g., find all `PENDING` tasks to assign).
- `test_messages_for_task`: Messages are scoped to a task ID, preventing message bleed between tasks.
- `test_activity_feed`: The activity feed aggregates events across all agents and tasks for audit logging.
- `test_undelivered_notifications`: Notifications accumulate until an agent reads them. This supports agents that are offline temporarily — they catch up on startup.
- `test_stats`: The store exposes aggregate statistics (agent count, task counts by status) for dashboards.
- `test_persistence`: After saving data, creating a new store instance pointed at the same directory loads the same data. This verifies that persistence is to disk, not just in-process memory.

## Manager (`TestMissionControlManager`)

`MissionControlManager` is the high-level facade used by agents and the API. It encapsulates store operations with business logic:

- `test_create_agent`: Creates an `AgentProfile` and persists it via the store.
- `test_create_task_with_assignment`: Creating a task with an assignee immediately sets the task's `assigned_to` field and posts a notification to the assigned agent.
- `test_update_task_status`: Status transitions (e.g., `PENDING → IN_PROGRESS`) are validated and persisted.
- `test_post_message_with_mentions` / `test_post_message_mention_all`: Posting a message triggers notifications to mentioned agents. `@all` is a special mention that notifies every registered agent — useful for broadcast announcements.
- `test_create_and_update_document`: Documents (shared outputs like reports or plans) are created and updated through the manager.
- `test_record_heartbeat`: Agents periodically call `record_heartbeat` to signal they are alive. The manager stores the timestamp, which the heartbeat daemon uses to detect stale agents.
- `test_generate_standup`: The manager can produce a standup summary — a snapshot of recent activity — for each agent.

## Singleton Reset Pattern

Every test fixture calls `reset_mission_control_store()` and `reset_mission_control_manager()` before creating fresh instances. These reset functions clear the module-level singletons. Without them, a singleton created in one test would leak into the next, causing flaky failures when test order changes.

## Known Gaps

No TODOs in this file. The suite does not cover concurrent writes to the file store, which could cause data corruption if two agents write simultaneously without file locking.
