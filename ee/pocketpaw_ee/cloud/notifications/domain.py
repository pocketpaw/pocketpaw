"""Domain value objects for notifications.

Pure-Python frozen dataclasses. No Beanie, no Pydantic, no FastAPI
imports. The repository layer converts between these and the Beanie
``Notification`` document; the DTO layer converts these to Pydantic
response models.

Why a separate `Notification` from the Beanie document: services should
operate on plain Python values so they can be tested without a Mongo
fixture. The Beanie document carries persistence concerns (indexes,
ObjectId management, snake_case Mongo field aliases) that services
shouldn't touch.

`NotificationSource` is structurally identical to the Beanie sub-model
of the same name; the repository converts field-by-field. We accept the
duplication for now because eliminating it would require changes to
``models/notification.py`` and its callers — out of scope for Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NotificationSource:
    """Pointer to the entity that triggered the notification (a message,
    comment, invite, etc.)."""

    type: str
    id: str
    pocket_id: str | None = None
    room_id: str | None = None


@dataclass(frozen=True)
class Notification:
    """In-app notification for a user."""

    id: str
    workspace_id: str
    recipient_id: str
    kind: str  # mention, comment, reply, invite, agent_complete, pocket_shared
    title: str
    body: str
    source: NotificationSource | None
    read: bool
    created_at: datetime
    expires_at: datetime | None = None


__all__ = ["Notification", "NotificationSource"]
