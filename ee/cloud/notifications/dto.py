"""Wire-format DTO for notifications.

The shape of `NotificationOut` matches the dict the legacy `_to_wire`
function produced byte-for-byte. Tests in `test_router_golden.py` lock
this in at the router boundary. Routers map domain → DTO via
``notification_to_dto`` instead of constructing dicts ad-hoc.
"""

from __future__ import annotations

from pydantic import BaseModel

from ee.cloud._core.time import iso_utc
from ee.cloud.notifications.domain import Notification


class NotificationOut(BaseModel):
    """Wire response shape for notifications."""

    id: str
    user_id: str
    workspace_id: str
    kind: str
    title: str
    body: str
    source_id: str | None
    source_type: str | None = None
    source_pocket_id: str | None = None
    source_room_id: str | None = None
    read: bool
    created_at: str | None


def notification_to_dto(n: Notification) -> NotificationOut:
    """Map a domain `Notification` to its wire DTO."""
    return NotificationOut(
        id=n.id,
        user_id=n.recipient_id,
        workspace_id=n.workspace_id,
        kind=n.kind,
        title=n.title,
        body=n.body,
        source_id=n.source.id if n.source else None,
        source_type=n.source.type if n.source else None,
        source_pocket_id=n.source.pocket_id if n.source else None,
        source_room_id=n.source.room_id if n.source else None,
        read=n.read,
        created_at=iso_utc(n.created_at),
    )


__all__ = ["NotificationOut", "notification_to_dto"]
