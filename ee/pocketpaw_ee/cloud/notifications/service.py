"""Notification service — CRUD + realtime fan-out.

Besides the in-app realtime ``emit(NotificationNew(...))``, new notifications
fan OUT of the app to the workspace's Slack / signed generic webhook. ``create``
only ENQUEUES those deliveries in the ``notification_outbox``
(``notifications/delivery.py`` + ``outbox.py``), so the request path never waits
on a remote endpoint; ``create_many`` enqueues in a bounded background task.
This service is the SOLE writer of the ``NotificationDeliveryConfig`` doc,
fronted by the PUT /notifications/delivery-config route; the webhook signing
secret is minted there and returned once.

Sole owner of writes to the ``Notification`` Beanie document. Writes are
inline; there is no separate repository layer. Tests use the shared
``mongo_db`` fixture (mongomock-motor) instead of injecting a Protocol
fake.

Public API is module-level ``async def`` functions:

- ``create(...)`` — insert a notification, emit ``NotificationNew``, fan out
- ``create_many(...)`` — the same for many recipients: one ``insert_many``, the
  external fan-out in a bounded background task (one config read per batch)
- ``list_for_user(user_id)`` — list domain ``Notification`` objects
- ``list_for_user_dicts(user_id)`` — list of legacy wire-format dicts
- ``mark_read(notification_id, user_id)`` — flip the read flag, emit
- ``clear_all(user_id)`` — bulk mark unread → read for a user, emit
- ``get_delivery_config(workspace_id)`` — read the external-delivery config
- ``set_delivery_config(workspace_id, ...)`` — upsert the external-delivery config
- ``webhook_target`` / ``record_webhook_result`` — the outbox's view of the
  workspace webhook (url + decrypted secret; failure counter, auto-disable)

Cross-module fan-out callers (``chat/message_service.py``,
``workspace/service.py``) call ``notifications_service.create(...)``
directly via module import.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    NotificationCleared,
    NotificationDeleted,
    NotificationNew,
    NotificationRead,
)
from pocketpaw_ee.cloud._core.realtime.fanout import map_bounded
from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
from pocketpaw_ee.cloud.models.notification import NotificationSource as _NotificationSourceDoc
from pocketpaw_ee.cloud.notifications.delivery import (
    enqueue_external,
    schedule_external_many,
    validate_webhook_url,
)
from pocketpaw_ee.cloud.notifications.domain import Notification, NotificationSource
from pocketpaw_ee.cloud.notifications.dto import notification_to_dto

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
        type=src.type,
        id=src.id,
        pocket_id=src.pocket_id,
        room_id=src.room_id,
        agent_id=getattr(src, "agent_id", None),
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
        type=src.type,
        id=src.id,
        pocket_id=src.pocket_id,
        room_id=src.room_id,
        agent_id=getattr(src, "agent_id", None),
    )


def _to_domain(doc: _NotificationDoc) -> Notification:
    return Notification(
        id=str(doc.id),
        workspace_id=doc.workspace,
        recipient_id=doc.recipient,
        actor_id=doc.actor,
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
    actor_id: str | None = None,
    deliver_external: bool = True,
) -> Notification:
    """Insert, emit ``NotificationNew`` (bell + push), and enqueue the workspace's
    external deliveries. A caller that routes the event to external sinks itself
    (the lead bridge, once per event) passes ``deliver_external=False``."""
    doc = _NotificationDoc(
        workspace=workspace_id,
        recipient=recipient,
        actor=actor_id,
        type=kind,
        title=title,
        body=body,
        source=_source_to_doc(source),
        read=False,
    )
    await doc.insert()
    created = _to_domain(doc)
    await emit(NotificationNew(data=notification_to_dto(created).model_dump()))
    # External fan-out (Slack / signed webhook) goes through the outbox.
    # Never-raise: a broken enqueue can't roll back the insert or the emit.
    if deliver_external:
        await enqueue_external(created)
    return created


async def create_many(
    *,
    workspace_id: str,
    recipients: list[str],
    kind: str,
    title: str,
    body: str = "",
    source: _NotificationSourceDoc | NotificationSource | None = None,
    actor_id: str | None = None,
    deliver_external: bool = True,
) -> list[Notification]:
    """``create`` for many recipients of the same notification: one
    ``insert_many``, one ``NotificationNew`` per recipient (as ``create`` emits),
    and ONE background external delivery for the batch (one config read, off
    the caller's request path). A caller creating several batches for one event
    passes ``deliver_external=False`` and hands them all to
    ``schedule_external_many`` itself, so the config is read once."""
    if not recipients:
        return []
    src = _source_to_doc(source)
    # Beanie's insert_many writes no ids back and skips the Insert hook, so the
    # id is minted here; ``createdAt`` comes from its default_factory.
    docs = [
        _NotificationDoc(
            id=PydanticObjectId(),
            workspace=workspace_id,
            recipient=recipient,
            actor=actor_id,
            type=kind,
            title=title,
            body=body,
            source=src,
            read=False,
        )
        for recipient in recipients
    ]
    await _NotificationDoc.insert_many(docs)
    created = [_to_domain(doc) for doc in docs]
    await map_bounded(
        created, lambda n: emit(NotificationNew(data=notification_to_dto(n).model_dump()))
    )
    if deliver_external:
        schedule_external_many(created)
    return created


async def has_recent(workspace_id: str, kind: str, since: datetime) -> bool:
    """Whether a ``kind`` notification was created in ``workspace_id`` since
    ``since`` (dedupe for once-a-day operational notices)."""
    doc = await _NotificationDoc.find_one(
        {"workspace": workspace_id, "type": kind, "createdAt": {"$gte": since}}
    )
    return doc is not None


async def count_unread(user_id: str) -> int:
    """Return the total count of unread notifications for a user."""
    return await _NotificationDoc.find({"recipient": user_id, "read": False}).count()


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


async def delete_notification(notification_id: str, user_id: str) -> bool:
    """Delete a single notification. Returns True if deleted, False if not found."""
    doc = await _NotificationDoc.get(PydanticObjectId(notification_id))
    if not doc or doc.recipient != user_id:
        return False
    await doc.delete()
    await emit(NotificationDeleted(data={"id": notification_id, "user_id": user_id}))
    return True


# ---------------------------------------------------------------------------
# External-delivery config — this service is the SOLE writer of the
# NotificationDeliveryConfig doc (import-linter "Notifications" contract).
# ---------------------------------------------------------------------------


_WEBHOOK_DISABLE_THRESHOLD = 10
# After a rotation the replaced secret keeps signing (as a second ``v1=``) this long.
WEBHOOK_SECRET_GRACE = timedelta(hours=24)


def _config_to_dict(doc, *, webhook_secret: str | None = None) -> dict:
    """Wire shape for the delivery config. The URLs are returned as stored (the
    admin who set them may see them); the signing secret only when
    ``webhook_secret`` is passed, i.e. once, right after it was minted.
    ``signed`` is False for a webhook saved before signing existed: it still
    delivers, unsigned, until the admin saves it again or rotates its secret."""
    return {
        "workspace_id": doc.workspace,
        "slack_webhook_url": doc.slack_webhook_url,
        "webhook_url": doc.webhook_url,
        "enabled": doc.enabled,
        "routes": dict(doc.routes or {}),
        "has_webhook_secret": bool(doc.webhook_secret_enc),
        "signed": bool(doc.webhook_url and doc.webhook_secret_enc),
        "webhook_secret": webhook_secret,
        "webhook_disabled_at": doc.webhook_disabled_at,
        "webhook_failure_count": doc.webhook_failure_count,
    }


async def _find_config(workspace_id: str):
    from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig

    return await NotificationDeliveryConfig.find_one(
        NotificationDeliveryConfig.workspace == workspace_id
    )


def _config_collection():
    from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig

    return NotificationDeliveryConfig.get_pymongo_collection()


async def get_delivery_config(workspace_id: str) -> dict | None:
    """Return the workspace's external-delivery config as a wire dict, or
    ``None`` when unset."""
    doc = await _find_config(workspace_id)
    return _config_to_dict(doc) if doc is not None else None


async def set_delivery_config(
    workspace_id: str,
    *,
    slack_webhook_url: str | None = None,
    webhook_url: str | None = None,
    enabled: bool = False,
    routes: dict[str, list[str]] | None = None,
) -> dict:
    """Upsert the workspace's external-delivery config and return the wire dict.

    A non-empty URL that fails the SSRF check (DNS included) is rejected with
    ``Forbidden`` before anything is stored. Empty / ``None`` clears that sink.
    Saving a NEW webhook URL, or one that has no secret yet, mints a signing
    secret returned in this response only. Any save that names a webhook URL
    re-arms a webhook that was switched off.
    """
    from pocketpaw_ee.cloud.audit.webhooks import mint_secret
    from pocketpaw_ee.cloud.auth.sso import crypto
    from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig

    slack = (slack_webhook_url or "").strip() or None
    generic = (webhook_url or "").strip() or None
    if slack is not None:
        await validate_webhook_url(slack)
    if generic is not None:
        await validate_webhook_url(generic)

    doc = await _find_config(workspace_id)
    if doc is None:
        doc = NotificationDeliveryConfig(workspace=workspace_id)
    new_secret: str | None = None
    if generic is None:
        doc.webhook_secret_enc = ""
        doc.webhook_secret_prev_enc = ""
    elif generic != doc.webhook_url or not doc.webhook_secret_enc:
        new_secret = mint_secret()
        doc.webhook_secret_enc = crypto.encrypt(new_secret)
        doc.webhook_secret_prev_enc = ""
    doc.webhook_failure_count = 0
    doc.webhook_disabled_at = None
    doc.slack_webhook_url = slack
    doc.webhook_url = generic
    doc.enabled = enabled
    doc.routes = dict(routes or {})
    await doc.save()
    return _config_to_dict(doc, webhook_secret=new_secret)


async def rotate_webhook_secret(workspace_id: str) -> dict | None:
    """Mint a new signing secret for the workspace webhook, returned once, and
    re-arm a webhook that was switched off. The replaced secret keeps signing
    alongside the new one for ``WEBHOOK_SECRET_GRACE``. None when no webhook."""
    from pocketpaw_ee.cloud.audit.webhooks import mint_secret
    from pocketpaw_ee.cloud.auth.sso import crypto

    doc = await _find_config(workspace_id)
    if doc is None or not doc.webhook_url:
        return None
    secret = mint_secret()
    await _config_collection().update_one(
        {"_id": doc.id},
        {
            "$set": {
                "webhook_secret_prev_enc": doc.webhook_secret_enc,
                "webhook_secret_enc": crypto.encrypt(secret),
                "webhook_secret_rotated_at": datetime.now(UTC),
                "webhook_failure_count": 0,
                "webhook_disabled_at": None,
            }
        },
    )
    doc = await _find_config(workspace_id)
    return _config_to_dict(doc, webhook_secret=secret)


def signing_secrets(current_enc: str, prev_enc: str, rotated_at: datetime | None) -> list[str]:
    """Decrypted secrets to sign with: the current one, then the replaced one
    while the rotation grace window is open. [] means deliver unsigned."""
    from pocketpaw_ee.cloud.auth.sso import crypto

    if not current_enc:
        return []
    out = [crypto.decrypt(current_enc)]
    if prev_enc and rotated_at is not None:
        at = rotated_at if rotated_at.tzinfo else rotated_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - at < WEBHOOK_SECRET_GRACE:
            out.append(crypto.decrypt(prev_enc))
    return out


async def webhook_target(workspace_id: str) -> tuple[str, list[str]] | None:
    """(url, signing secrets) of the workspace webhook while it is configured,
    enabled and not switched off; None otherwise. An empty secret list is a
    pre-signing webhook: the outbox delivers it unsigned, as it always was."""
    doc = await _find_config(workspace_id)
    if doc is None or not doc.enabled or not doc.webhook_url:
        return None
    if doc.webhook_disabled_at is not None:
        return None
    return doc.webhook_url, signing_secrets(
        doc.webhook_secret_enc, doc.webhook_secret_prev_enc, doc.webhook_secret_rotated_at
    )


async def slack_target(workspace_id: str) -> str | None:
    """The workspace Slack URL while configured and enabled (send-time check)."""
    doc = await _find_config(workspace_id)
    if doc is None or not doc.enabled:
        return None
    return doc.slack_webhook_url or None


async def record_webhook_result(workspace_id: str, *, ok: bool) -> None:
    """Reset the consecutive-failure counter on success; on a dead delivery bump
    it and switch the webhook off at ``_WEBHOOK_DISABLE_THRESHOLD``. Atomic
    ``$inc`` / conditional ``$set``, never a whole-document save, so concurrent
    results and a concurrent config save can't lose each other's updates."""
    coll = _config_collection()
    if ok:
        await coll.update_one(
            {"workspace": workspace_id, "webhook_failure_count": {"$gt": 0}},
            {"$set": {"webhook_failure_count": 0}},
        )
        return
    await coll.update_one({"workspace": workspace_id}, {"$inc": {"webhook_failure_count": 1}})
    await coll.update_one(
        {
            "workspace": workspace_id,
            "webhook_failure_count": {"$gte": _WEBHOOK_DISABLE_THRESHOLD},
            "webhook_disabled_at": None,
        },
        {"$set": {"webhook_disabled_at": datetime.now(UTC)}},
    )


__all__ = [
    "Notification",
    "NotificationSource",
    "count_unread",
    "create",
    "delete_notification",
    "has_recent",
    "get_delivery_config",
    "list_for_user",
    "list_for_user_dicts",
    "mark_read",
    "clear_all",
    "record_webhook_result",
    "rotate_webhook_secret",
    "set_delivery_config",
    "signing_secrets",
    "slack_target",
    "webhook_target",
]
