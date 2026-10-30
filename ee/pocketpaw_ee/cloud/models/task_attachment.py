"""TaskAttachment document — files linked to a Task.

Each attachment references an uploaded file (via ``file_id`` pointing to
a ``FileUpload`` document) and stores denormalised metadata so list
queries don't need a join. The actual file bytes live in the StorageAdapter
behind the uploads service.

Only ``ee.cloud.tasks.service`` may import this module; the import-linter
contract enforces the rule.
"""

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class TaskAttachment(TimestampedDocument):
    """A file attached to a Task.

    ``file_id`` references the ``FileUpload`` document in the uploads
    provider. ``filename``, ``mime``, and ``size`` are denormalised so
    the attachment list in the work-item detail panel can render without
    a cross-collection query.
    """

    task_id: Indexed(str)  # type: ignore[valid-type]
    workspace_id: str
    creator_id: str
    file_id: str
    filename: str
    mime: str
    size: int

    class Settings:
        name = "task_attachments"
        indexes = [
            [("task_id", 1), ("createdAt", -1)],
            [("workspace_id", 1), ("createdAt", -1)],
        ]


__all__ = ["TaskAttachment"]
