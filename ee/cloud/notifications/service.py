"""Notification service — CRUD + realtime fan-out.

Sole owner of writes to the ``Notification`` Beanie document. Writes are
inline; there is no separate repository layer. Tests use the shared
``mongo_db`` fixture (mongomock-motor) instead of injecting a Protocol
fake.

Public API is module-level ``async def`` functions:

- ``create(...)`` — insert a notification, emit ``NotificationNew``
- ``list_for_user(user_id)`` — list domain ``Notification`` objects
- ``list_for_user_dicts(user_id)`` — list of legacy wire-format dicts
- ``mark_read(notification_id, user_id)`` — flip the read flag, emit
- ``clear_all(user_id)`` — bulk mark unread → read for a user, emit

Cross-module fan-out callers (``chat/message_service.py``,
``workspace/service.py``) call ``notifications_service.create(...)``
directly via module import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from beanie import PydanticObjectId

from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    NotificationCleared,
    NotificationNew,
    NotificationRead,
)
from ee.cloud.models.notification import Notification as _NotificationDoc
from ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc
from ee.cloud.notifications.domain import Notification, NotificationSource
from ee.cloud.notifications.dto import notification_to_dto

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Private mapping helpers — Beanie doc ↔ domain
# ---------------------------------------------------------------------------


def _source_to_domain(
    src: _NotificationSourceDoc | None,
) -> NotificationSource | None:
    if src is None:
        return None
    return NotificationSource(
        type=src.type, id=src.id, pocket_id=src.pocket_id, room_id=src.room_id
    )


def _source_to_doc(
    src: NotificationSource | _NotificationSourceDoc | None,
) -> _NotificationSourceDoc | None:
    """Accept either domain or doc form (legacy callers pass doc form)."""
    if src is None:
        return None
    if isinstance(src, _NotificationSourceDoc):
        return src
    return _NotificationSourceDoc(
        type=src.type, id=src.id, pocket_id=src.pocket_id, room_id=src.room_id
    )


def _to_domain(doc: _NotificationDoc) -> Notification:
    return Notification(
        id=str(doc.id),
        workspace_id=doc.workspace,
        recipient_id=doc.recipient,
        kind=doc.type,  # Beanie field is `type`; domain renames to `kind`
        title=doc.title,
        body=doc.body,
        source=_source_to_domain(doc.source),
        read=doc.read,
        # Beanie reads return naive datetimes via TimestampedDocument's
        # ``createdAt`` (camelCase Mongo field). Always populated in
        # practice; type:ignore covers the getattr fallback.
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        expires_at=doc.expires_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create(
    *,
    workspace_id: str,
    recipient: str,
    kind: str,
    title: str,
    body: str = "",
    source: _NotificationSourceDoc | NotificationSource | None = None,
) -> Notification:
    doc = _NotificationDoc(
        workspace=workspace_id,
        recipient=recipient,
        type=kind,
        title=title,
        body=body,
        source=_source_to_doc(source),
        read=False,
    )
    await doc.insert()
    created = _to_domain(doc)
    await emit(NotificationNew(data=notification_to_dto(created).model_dump()))
    return created


async def count_unread(user_id: str) -> int:
    """Return the total count of unread notifications for a user."""
    return await _NotificationDoc.find(
        {"recipient": user_id, "read": False}
    ).count()


async def list_for_user(
    user_id: str, *, unread: bool = False, limit: int = 50
) -> list[Notification]:
    query: dict = {"recipient": user_id}
    if unread:
        query["read"] = False
    cursor = (
        _NotificationDoc.find(query)
        .sort(-_NotificationDoc.createdAt)  # type: ignore[operator]
        .limit(limit)
    )
    return [_to_domain(doc) async for doc in cursor]


async def list_for_user_dicts(user_id: str, *, unread: bool = False, limit: int = 50) -> list[dict]:
    """Wire-shape variant: returns ``list[dict]`` for legacy callers
    that haven't yet adopted the DTO."""
    notes = await list_for_user(user_id, unread=unread, limit=limit)
    return [notification_to_dto(n).model_dump() for n in notes]


async def mark_read(notification_id: str, user_id: str) -> bool:
    doc = await _NotificationDoc.get(PydanticObjectId(notification_id))
    if not doc or doc.recipient != user_id:
        return False
    if doc.read:
        return False
    doc.read = True
    await doc.save()
    await emit(NotificationRead(data={"id": notification_id, "user_id": user_id}))
    return True


async def clear_all(user_id: str) -> int:
    result = await _NotificationDoc.find({"recipient": user_id, "read": False}).update_many(
        {"$set": {"read": True}}
    )
    count = getattr(result, "modified_count", 0)
    await emit(NotificationCleared(data={"user_id": user_id}))
    return count


__all__ = [
    "Notification",
    "NotificationSource",
    "count_unread",
    "create",
    "list_for_user",
    "list_for_user_dicts",
    "mark_read",
    "clear_all",
]
