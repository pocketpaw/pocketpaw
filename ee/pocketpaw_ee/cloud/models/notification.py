"""Notification document.

Updated: 2026-08-11 (fix/notif-liveness-dispatch) — the inbox index named
``created_at``, a field this document does not have: timestamps come from
``TimestampedDocument`` as camelCase ``createdAt`` (no alias), so the sort key
pointed at nothing and the bell's newest-first query fell back to a collection
scan. Fixed to ``createdAt``, and a TTL index added so notification rows age
out at 90 days like their five sibling collections instead of accumulating
forever.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import BaseModel
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class NotificationSource(BaseModel):
    type: str
    id: str
    pocket_id: str | None = None
    room_id: str | None = None
    # The agent this source belongs to, when it belongs to one. The concierge
    # kinds set it: their inbox lives on an AGENT, so without it a client has
    # no id to build a link from and falls back to the chat surface.
    agent_id: str | None = None


class Notification(TimestampedDocument):
    """In-app notification for a user."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    recipient: Indexed(str)  # type: ignore[valid-type]
    actor: str | None = None  # user id of the person who triggered this notification
    type: str  # notification type: mention, comment, reply, invite, agent_complete,
    # pocket_shared, meeting_scheduled, meeting_cancelled, meeting_started, meeting_reminder
    title: str
    body: str = ""
    source: NotificationSource | None = None
    read: bool = False
    # DEAD FIELD — nothing writes it. The sole writer (notifications/service.py
    # ``create``) never sets it, so it is None on every row and can back no TTL.
    # Expiry runs off ``createdAt`` below. Kept for now so existing documents
    # deserialize; removal is deferred to a dedicated cleanup.
    expires_at: datetime | None = None

    class Settings:
        name = "notifications"
        indexes = [
            # The bell's DEFAULT list query (list_for_user with unread=False):
            # filters recipient only, sorts newest first. The three-key index
            # below cannot serve this — skipping a middle key leaves ``read``
            # as a gap, so the sort can't ride the index.
            IndexModel([("recipient", 1), ("createdAt", -1)]),
            # The unread variant: recipient + read, newest first. Its
            # (recipient, read) prefix also serves count_unread.
            # Sorts on ``createdAt`` — TimestampedDocument's camelCase field,
            # NOT ``created_at``, which does not exist on this document.
            IndexModel([("recipient", 1), ("read", 1), ("createdAt", -1)]),
            # Mongo auto-deletes notifications older than 90 days. Nobody reads
            # a three-month-old bell item, and the collection is append-heavy.
            IndexModel([("createdAt", 1)], expireAfterSeconds=86400 * 90),
        ]
