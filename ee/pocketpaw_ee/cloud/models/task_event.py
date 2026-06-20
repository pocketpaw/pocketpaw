"""TaskEvent document — comments and activity on a Task."""

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class TaskEvent(TimestampedDocument):
    """A comment or activity entry on a task (persistent, realtime)."""

    workspace_id: Indexed(str)  # type: ignore[valid-type]
    task_id: Indexed(str)  # type: ignore[valid-type]
    author_id: str
    author_name: str
    body: str

    class Settings:
        name = "task_events"
        indexes = [
            [("task_id", 1), ("created_at", -1)],
        ]


__all__ = ["TaskEvent"]
