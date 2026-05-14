# listeners.py — in-process bus subscribers for Tasks.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend.
#   Bridges ``task.proposed`` into the existing ``notifications/``
#   surface so a human assignee gets an in-app notification when a Task
#   lands in their queue. Agent assignees are skipped — they poll their
#   own queue via ``list_my_tasks`` / SSE and don't need a notification
#   row.
"""Tasks bus subscribers.

The Tasks service emits :class:`TaskProposed` on every create. This
module subscribes that event and routes a notification to human
assignees only. Wired into the bus at startup from
``ee.cloud.__init__:mount_cloud`` after ``init_realtime`` has installed
the singleton bus.
"""

from __future__ import annotations

import logging

from ee.cloud._core.realtime.bus import get_bus
from ee.cloud._core.realtime.events import Event, TaskProposed

logger = logging.getLogger(__name__)


def _extract_task(event: Event) -> dict | None:
    """Pull the task dict from a TaskProposed event payload, or ``None``
    when the payload is malformed (defensive — the bus should never
    deliver a malformed event, but a broken handler must not be able to
    take down the rest of the subscriber chain)."""

    data = getattr(event, "data", None) or {}
    task = data.get("task")
    if not isinstance(task, dict):
        return None
    return task


async def notify_human_assignee(event: Event) -> None:
    """Create an in-app notification when a Task is proposed to a human.

    Agent assignees are skipped on purpose — the agent runtime picks up
    its work via ``list_my_tasks`` polling or the ``task.proposed`` SSE
    event directly. Routing them through the human notification surface
    would double-fire and clutter the operator's notification feed.
    """

    task = _extract_task(event)
    if task is None:
        return

    assignee = task.get("assignee") or {}
    if assignee.get("kind") != "human":
        return

    recipient = assignee.get("id")
    workspace_id = task.get("workspace_id")
    if not recipient or not workspace_id:
        return

    # Don't notify someone of work they assigned to themselves — they
    # already know about it. The Mission Control feed will show the row
    # anyway via the realtime ``task.proposed`` push.
    if recipient == task.get("creator_id"):
        return

    title = task.get("title") or "New task"
    creator_id = task.get("creator_id") or ""
    body = f"Assigned by {creator_id}".strip()

    try:
        from ee.cloud.notifications import service as notifications_service

        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind="task_assigned",
            title=title,
            body=body,
            source=None,
        )
    except Exception:
        logger.warning("task.proposed → notification fan-out failed", exc_info=True)


def register_task_listeners() -> None:
    """Wire the Tasks subscribers into the bus.

    Called once from ``mount_cloud`` after ``init_realtime`` has set the
    singleton. Idempotent at the framework level only — calling twice
    would double-register; the bootstrap path calls it exactly once.
    """

    bus = get_bus()
    bus.subscribe(TaskProposed.EVENT_TYPE, notify_human_assignee)


__all__ = ["notify_human_assignee", "register_task_listeners"]
