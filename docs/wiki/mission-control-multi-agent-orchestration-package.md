---
{
  "title": "Mission Control — Multi-Agent Orchestration Package",
  "summary": "The `pocketpaw.mission_control` package provides a shared workspace for multiple AI agents to collaborate on tasks, including agent profiles with status tracking, a task lifecycle engine, threaded messaging with @mentions, document storage, activity feeds, and real-time WebSocket streaming of task execution events.",
  "concepts": [
    "Mission Control",
    "multi-agent orchestration",
    "task lifecycle",
    "AgentProfile",
    "MCTaskExecutor",
    "HeartbeatDaemon",
    "WebSocket streaming",
    "activity feed",
    "notifications",
    "@mentions",
    "FileMissionControlStore"
  ],
  "categories": [
    "Multi-Agent Orchestration",
    "Task Management"
  ],
  "source_docs": [
    "0637b7c90f1b50e0"
  ],
  "backlinks": null,
  "word_count": 437,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Mission Control (MC) was created to solve a coordination problem: as PocketPaw supports more agent backends and more concurrent tasks, there is no natural place for agents to share state, hand off work, or surface progress to the user. MC provides that shared workspace.

It was created on 2026-02-05 and extended the same day with `MCTaskExecutor` for real agent execution with WebSocket streaming.

## Package Components

| Module | Role |
|--------|------|
| `models` | Dataclasses: `AgentProfile`, `Task`, `Message`, `Activity`, `Document`, `Notification` |
| `store` | `FileMissionControlStore` — JSON persistence layer |
| `manager` | `MissionControlManager` — business logic facade |
| `api` | FastAPI router — REST endpoints |
| `executor` | `MCTaskExecutor` — runs agents on tasks, streams output |
| `heartbeat` | `HeartbeatDaemon` — detects stale/offline agents |

## Task Lifecycle

```
inbox -> assigned -> in_progress -> review -> done
                                          \-> failed
```

Status transitions are managed by `MissionControlManager.update_task_status` and reflected in the activity feed automatically. The explicit `review` state exists because multi-agent workflows often need a human or lead agent to approve deliverables before marking a task done.

## Reset Functions

Every singleton (`MissionControlManager`, `MCTaskExecutor`, `HeartbeatDaemon`, `FileMissionControlStore`) exposes a `reset_*` function. These exist for testing: unit tests can reset singletons between cases without restarting the process.

## Real-Time Streaming

Task execution events are broadcast as WebSocket messages:

- `mc_task_started` — execution begins
- `mc_task_output` — agent produces a chunk
- `mc_task_completed` — execution ends

The dashboard subscribes to these events to show live agent output without polling.

## @Mention Notifications

When a message is posted with `@mentions`, MC creates `Notification` records for the mentioned agents. Agents poll `/api/mission-control/notifications?undelivered_only=true` or receive push notifications via the heartbeat system.

## Why a Shared Workspace Model?

Traditional task queues (Celery, RQ, etc.) distribute work to anonymous workers. Mission Control takes a different approach: agents have persistent identities (`AgentProfile`) with names, roles, specialties, and status. Tasks have threads where agents can post updates, ask questions, and share documents. This makes multi-agent workflows observable: a human monitoring the dashboard can see exactly which agent is working on what, read its messages, and intervene if needed.

The model is inspired by project management tools (Linear, Jira) rather than job queues, because AI agents doing complex tasks behave more like team members than background workers. They need to communicate, produce deliverables, and be accountable for their work.

## Known Gaps

No cross-agent data isolation: all agents in MC share the same store and can read each other's tasks and messages. Access control is not implemented. The heartbeat daemon detects offline agents but does not automatically reassign their tasks.