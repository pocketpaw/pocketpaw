---
{
  "title": "Deep Work Human Task Router — Multi-Channel Notification Fanout",
  "summary": "HumanTaskRouter bridges the Deep Work execution layer and the user by pushing human-required tasks and project lifecycle events to all configured messaging channels via MessageBus broadcast. It covers four notification types: human tasks needing action, agent-completed tasks awaiting review, plans ready for approval, and project completion summaries.",
  "concepts": [
    "HumanTaskRouter",
    "MessageBus",
    "broadcast_outbound",
    "human tasks",
    "task notifications",
    "plan approval",
    "project completion",
    "channel adapters",
    "Telegram",
    "Discord",
    "Deep Work notifications"
  ],
  "categories": [
    "deep-work",
    "messaging",
    "notifications",
    "channel-adapters"
  ],
  "source_docs": [
    "1b218030b82b3832"
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

In an automated Deep Work project, most tasks execute autonomously through agents. But some tasks require a human — they can't be delegated to an AI. `HumanTaskRouter` is the mechanism that reaches out to the actual user across whatever channels they've configured (Telegram, Discord, Slack, WhatsApp, WebSocket) and delivers an actionable notification.

The router is deliberately channel-agnostic. It doesn't know about Telegram or Discord directly — it calls `MessageBus.broadcast_outbound`, which fans the message out to all active adapters. This design means adding a new channel adapter automatically gives HumanTaskRouter delivery on that channel without any changes here.

## Notification Types

### `notify_human_task(task)`

Used when a task in a Deep Work project has `assign_to = "human"` (or equivalent). The agent pipeline cannot proceed until the human acts. The notification is formatted as a Markdown message with task title, truncated description (300 chars max), priority, and tags.

The 300-char truncation matters: task descriptions can be verbose PRD excerpts. Sending a wall of text to a Telegram chat is counterproductive.

### `notify_review_task(task)`

Used when an agent completes a task that requires human review before the project can continue. Different from `notify_human_task` — the work is done, the human just needs to sign off.

### `notify_plan_ready(project, task_count, estimated_minutes)`

Sent when `DeepWorkSession` finishes the planning phase and the project enters `AWAITING_APPROVAL` status. Gives the user a count of tasks and time estimate, with a prompt to approve in the dashboard. Without this notification, the project would silently sit in approval limbo — the user might not know planning completed.

### `notify_project_completed(project, tasks)`

Sent when the DependencyScheduler determines all tasks are resolved. Includes a `completed_count / total_count` ratio rather than just a completion message, giving the user signal about whether any tasks were skipped.

## `_publish_outbound` and Metadata

The private `_publish_outbound` method wraps `MessageBus.broadcast_outbound`. It attaches a typed metadata dictionary to each message (`type`, `task_id`, `project_id`) so channel adapters can react intelligently — for example, a WebSocket adapter might use `type: "plan_ready"` to trigger a UI update rather than just displaying the text.

## Known Gaps

- The `MessageBus` is accessed via a module-level import at call time, which means the router has an implicit dependency on bus initialization order. If the bus is not initialized when a notification fires, the call will fail silently (the bus getter typically returns a no-op bus in this scenario, but this is not documented).
- There is no retry or fallback mechanism if `broadcast_outbound` fails — a failed delivery means the human never receives the task notification.
- `notify_project_completed` passes an empty list as default for `tasks`, so the `completed_count / total_count` display shows `0/0` if the caller omits the tasks argument.
