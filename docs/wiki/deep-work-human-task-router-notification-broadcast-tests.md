---
{
  "title": "Deep Work Human Task Router: Notification Broadcast Tests",
  "summary": "This test suite validates the HumanTaskRouter, which bridges the automated Deep Work engine and human operators by publishing structured notifications over the MessageBus. It covers task notifications, review routing, plan-ready announcements, and project completion summaries, including edge cases like missing project IDs and empty task lists.",
  "concepts": [
    "HumanTaskRouter",
    "human_tasks",
    "OutboundMessage",
    "MessageBus",
    "broadcast_outbound",
    "notify_human_task",
    "notify_review_task",
    "notify_plan_ready",
    "notify_project_completed",
    "task_notification",
    "Deep_Work",
    "description_truncation",
    "project_id_default"
  ],
  "categories": [
    "testing",
    "deep-work",
    "human-tasks",
    "message-bus",
    "test"
  ],
  "source_docs": [
    "861af8c9ae9c42eb"
  ],
  "backlinks": null,
  "word_count": 489,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deep_work_human_tasks.py` tests the `HumanTaskRouter` class from `pocketpaw.deep_work.human_tasks`. In PocketPaw's Deep Work feature, not all tasks are handled by AI agents—some require human action (uploading assets, approving PRDs, providing credentials). The `HumanTaskRouter` is the component responsible for notifying human operators via the outbound message bus when their attention is needed.

## Why This Module Exists

Deep Work sessions mix AI-executed tasks with human-gated tasks. Without a reliable notification layer, human tasks would silently block the dependency graph—agents would wait for upstream human work to complete, but no one would know they were needed. The `HumanTaskRouter` prevents this by broadcasting `OutboundMessage` events to connected channels (Slack, Discord, email) whenever a human action is required.

## notify_human_task

`test_notify_human_task_publishes_outbound` verifies that calling `notify_human_task()` causes the bus to broadcast an `OutboundMessage` with correct content (task title, description, priority) and metadata (project ID, task ID, channel). The fixture uses a mock `MessageBus` with `broadcast_outbound` captured as an `AsyncMock`.

`test_notify_human_task_no_project_id` tests the edge case where a task has no associated project. Rather than crashing, the router defaults `project_id` to an empty string, allowing the notification to still reach the operator.

## notify_review_task

Review tasks are a distinct task type—they require a human to inspect agent output and approve or reject it. `test_notify_review_task_publishes_correct_message` confirms the router emits a different message format for review tasks vs. plain human tasks, ensuring the operator understands the action required.

## notify_plan_ready

After the planner generates a project plan, the session waits for human approval before dispatching tasks. `notify_plan_ready` broadcasts a summary notification containing the project title, task count, and estimated total minutes.

- `test_notify_plan_ready_includes_project_and_counts` verifies all three fields appear in the message.
- `test_notify_plan_ready_defaults` checks that zero task count and zero minutes are handled gracefully without crashing.

This notification is the signal that unlocks human approval, making it a critical path in the Deep Work workflow.

## notify_project_completed

When all tasks in a project reach DONE status, the router broadcasts a completion summary. `test_notify_project_completed_includes_counts` verifies it correctly counts completed tasks. `test_notify_project_completed_no_tasks` tests the `None` tasks case—without this guard, calling `len(None)` would raise a `TypeError` and silently swallow the completion notification.

## _format_task_notification

The private `_format_task_notification` helper constructs the human-readable message body. Three tests cover it:

- **All fields present**: title, description, priority tag, and custom tags all appear in output.
- **Long description truncation**: descriptions over 300 characters are truncated. This prevents notification messages from being wall-of-text dumps that overwhelm human operators in Slack/Discord where character limits or readability matter.
- **No description**: empty description is handled without introducing null pointer errors.

## Test Infrastructure

Tests use `AsyncMock` for the `MessageBus` and pytest fixtures for the router and sample data objects. All async tests follow the `pytest-asyncio` convention used throughout PocketPaw.

## Known Gaps

The tests mock the bus entirely—there is no integration test confirming that a real Discord or Slack channel adapter receives and renders the notification correctly. Channel-specific formatting (Markdown vs. plain text) is not tested here.
