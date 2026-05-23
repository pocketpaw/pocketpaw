"""Chat domain — message business logic (CRUD, reactions, threads, pins, search).

Sole owner of writes to the ``Message`` Beanie document. Module-level
``async def`` API. The doc → domain mapping helpers (formerly in
``repositories.py``) live alongside the public API as private helpers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import cast

from beanie import PydanticObjectId

from pocketpaw_ee.cloud.chat import group_service, unread_service
from pocketpaw_ee.cloud.chat.domain import Attachment as _AttachmentDomain
from pocketpaw_ee.cloud.chat.domain import Mention as _MentionDomain
from pocketpaw_ee.cloud.chat.domain import Message as _MessageDomain
from pocketpaw_ee.cloud.chat.domain import Reaction as _ReactionDomain
from pocketpaw_ee.cloud.chat.group_service import (
    _get_group_or_404,
    _require_can_post,
    _require_domain_group_admin,
    _require_group_member,
)
from pocketpaw_ee.cloud.chat.schemas import (
    EditMessageRequest,
    SendMessageRequest,
)
from pocketpaw_ee.cloud.models.message import Attachment as _AttachmentDoc
from pocketpaw_ee.cloud.models.message import Mention as _MentionDoc
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc
from pocketpaw_ee.cloud.models.message import Reaction as _ReactionDoc
from pocketpaw_ee.cloud.models.notification import NotificationSource
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.realtime.emit import emit
from pocketpaw_ee.cloud.realtime.events import (
    MessageDeleted,
    MessageEdited,
    MessageNew,
    MessageReaction,
    MessageSent,
    MessageUiStateUpdated,
    ThreadReply,
    UnreadUpdate,
)
from pocketpaw_ee.cloud.shared.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.shared.events import event_bus
from pocketpaw_ee.cloud.shared.time import iso_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Doc → domain mapping (formerly in repositories.py)
# ---------------------------------------------------------------------------


def _mention_to_domain(m: _MentionDoc) -> _MentionDomain:
    return _MentionDomain(type=m.type, id=m.id, display_name=m.display_name)


def _attachment_to_domain(a: _AttachmentDoc) -> _AttachmentDomain:
    return _AttachmentDomain(
        type=a.type, url=a.url, name=a.name, meta=tuple((k, v) for k, v in a.meta.items())
    )


def _reaction_to_domain(r: _ReactionDoc) -> _ReactionDomain:
    return _ReactionDomain(emoji=r.emoji, users=tuple(r.users))


def _message_doc_to_domain(doc: _MessageDoc) -> _MessageDomain:
    return _MessageDomain(
        id=str(doc.id),
        context_type=doc.context_type or "group",
        workspace_id=doc.workspace_id,
        group=doc.group,
        sender=doc.sender,
        sender_type=doc.sender_type,
        agent=doc.agent,
        content=doc.content,
        mentions=tuple(_mention_to_domain(m) for m in doc.mentions),
        reply_to=doc.reply_to,
        thread_count=doc.thread_count,
        thread_id=getattr(doc, "thread_id", None),
        is_thread_parent=getattr(doc, "is_thread_parent", False),
        attachments=tuple(_attachment_to_domain(a) for a in doc.attachments),
        reactions=tuple(_reaction_to_domain(r) for r in doc.reactions),
        edited=doc.edited,
        edited_at=doc.edited_at,
        deleted=doc.deleted,
        session_key=doc.session_key,
        role=doc.role,
        created_at=getattr(doc, "createdAt", None),
    )


# ---------------------------------------------------------------------------
# Internal Beanie ops (formerly MongoMessageRepository methods)
# ---------------------------------------------------------------------------


async def _message_get_domain(message_id: str) -> _MessageDomain | None:
    try:
        doc = await _MessageDoc.get(PydanticObjectId(message_id))
    except Exception:
        return None
    return _message_doc_to_domain(doc) if doc else None


async def _message_get_many_domain(message_ids: list[str]) -> list[_MessageDomain]:
    """Batch fetch messages by ID — used to prefetch reply parents
    without an N+1 round-trip per message."""
    if not message_ids:
        return []
    oids: list[PydanticObjectId] = []
    for mid in message_ids:
        try:
            oids.append(PydanticObjectId(mid))
        except Exception:
            continue
    if not oids:
        return []
    docs = await _MessageDoc.find({"_id": {"$in": oids}}).to_list()
    return [_message_doc_to_domain(d) for d in docs]


async def _list_for_group_paged(
    group_id: str,
    *,
    before_time: datetime | None = None,
    before_id: str | None = None,
    limit: int = 50,
    include_deleted: bool = False,
) -> list[_MessageDomain]:
    """Cursor-paginated history newest-first.

    Pagination uses ``(createdAt, _id)`` as a composite cursor so
    same-second messages don't get skipped or repeated.
    """
    query: dict = {"context_type": "group", "group": group_id}
    if not include_deleted:
        query["deleted"] = False
    if before_time is not None and before_id is not None:
        try:
            cursor_oid = PydanticObjectId(before_id)
        except Exception:
            cursor_oid = None
        if cursor_oid is not None:
            query["$or"] = [
                {"createdAt": {"$lt": before_time}},
                {"createdAt": before_time, "_id": {"$lt": cursor_oid}},
            ]
    cursor = (
        _MessageDoc.find(query)
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .limit(limit)
    )
    return [_message_doc_to_domain(d) async for d in cursor]


async def _list_replies(parent_message_id: str) -> list[_MessageDomain]:
    """All non-deleted group-context replies to a parent, oldest first."""
    cursor = _MessageDoc.find(
        {
            "context_type": "group",
            "reply_to": parent_message_id,
            "deleted": False,
        }
    ).sort([("createdAt", 1)])  # type: ignore[list-item]
    return [_message_doc_to_domain(d) async for d in cursor]


async def _search_in_group(group_id: str, query: str, *, limit: int = 100) -> list[_MessageDomain]:
    pattern = re.escape(query)
    docs = (
        await _MessageDoc.find(
            {
                "context_type": "group",
                "group": group_id,
                "deleted": False,
                "content": {"$regex": pattern, "$options": "i"},
            }
        )
        .limit(limit)
        .to_list()
    )
    return [_message_doc_to_domain(d) for d in docs]


async def _search_in_groups(
    group_ids: list[str], query: str, *, limit: int = 100
) -> list[_MessageDomain]:
    """Workspace-scoped search across many groups in one query, sorted newest-first."""
    if not group_ids:
        return []
    pattern = re.escape(query)
    docs = (
        await _MessageDoc.find(
            {
                "context_type": "group",
                "group": {"$in": group_ids},
                "deleted": False,
                "content": {"$regex": pattern, "$options": "i"},
            }
        )
        .sort([("createdAt", -1)])  # type: ignore[list-item]
        .limit(limit)
        .to_list()
    )
    return [_message_doc_to_domain(d) for d in docs]


async def _create_group_message_doc(
    *,
    group_id: str,
    sender: str | None,
    sender_type: str,
    content: str,
    agent: str | None = None,
    mentions: list[dict] | None = None,
    attachments: list[dict] | None = None,
    reply_to: str | None = None,
    thread_id: str | None = None,
    is_thread_parent: bool = False,
) -> _MessageDomain:
    """Insert a new group-context message."""
    mention_docs = [_MentionDoc(**m) for m in mentions or []]
    attachment_docs = [_AttachmentDoc(**a) for a in attachments or []]
    doc = _MessageDoc(
        context_type="group",
        group=group_id,
        sender=sender,
        sender_type=sender_type,
        agent=agent,
        content=content,
        mentions=mention_docs,
        attachments=attachment_docs,
        reply_to=reply_to,
        thread_id=thread_id,
        is_thread_parent=is_thread_parent,
    )
    await doc.insert()
    return _message_doc_to_domain(doc)


async def _edit_message_content(
    message_id: str, content: str, *, edited_at: datetime
) -> _MessageDomain:
    doc = await _MessageDoc.get(PydanticObjectId(message_id))
    if doc is None:
        raise NotFound("message", message_id)
    doc.content = content
    doc.edited = True
    doc.edited_at = edited_at
    await doc.save()
    return _message_doc_to_domain(doc)


async def _soft_delete_message(message_id: str) -> _MessageDomain:
    doc = await _MessageDoc.get(PydanticObjectId(message_id))
    if doc is None:
        raise NotFound("message", message_id)
    doc.deleted = True
    await doc.save()
    return _message_doc_to_domain(doc)


async def _toggle_reaction_doc(
    message_id: str, user_id: str, emoji: str
) -> tuple[_MessageDomain, bool]:
    """Toggle ``(user_id, emoji)`` on the message's reactions.

    Returns ``(updated_message, added)`` where ``added`` is True if the
    reaction was newly added, False if it was removed. An emoji with no
    users left is dropped from the reactions array.
    """
    doc = await _MessageDoc.get(PydanticObjectId(message_id))
    if doc is None:
        raise NotFound("message", message_id)

    existing = next((r for r in doc.reactions if r.emoji == emoji), None)
    added = True
    if existing is not None:
        if user_id in existing.users:
            existing.users.remove(user_id)
            if not existing.users:
                doc.reactions.remove(existing)
            added = False
        else:
            existing.users.append(user_id)
    else:
        doc.reactions.append(_ReactionDoc(emoji=emoji, users=[user_id]))

    await doc.save()
    return _message_doc_to_domain(doc), added


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REPLY_PREVIEW_CHARS = 140


def _reply_preview(parent: _MessageDoc | None) -> dict | None:
    """Build a small preview payload for an inline reply quote."""
    if parent is None or parent.deleted:
        return None
    snippet = (parent.content or "")[:_REPLY_PREVIEW_CHARS]
    return {
        "id": str(parent.id),
        "content": snippet,
        "sender": parent.sender,
        "senderType": parent.sender_type,
        "agent": parent.agent,
    }


def _message_response(msg: _MessageDoc, *, parent: _MessageDoc | None = None) -> dict:
    """Convert a Message Beanie document to a frontend-compatible dict."""
    return {
        "_id": str(msg.id),
        "group": msg.group,
        "sender": msg.sender,
        "senderType": msg.sender_type,
        "agent": msg.agent,
        "content": msg.content,
        "mentions": [m.model_dump() for m in msg.mentions],
        "replyTo": msg.reply_to,
        "replyPreview": _reply_preview(parent) if msg.reply_to else None,
        "threadCount": msg.thread_count,
        "threadId": getattr(msg, "thread_id", None),
        "isThreadParent": getattr(msg, "is_thread_parent", False),
        "attachments": [a.model_dump() for a in msg.attachments],
        "reactions": [r.model_dump() for r in msg.reactions],
        "edited": msg.edited,
        "editedAt": iso_utc(msg.edited_at),
        "deleted": msg.deleted,
        "createdAt": iso_utc(msg.createdAt),
    }


async def _get_group_message_or_404(message_id: str) -> _MessageDoc:
    """Load a non-deleted group-context Beanie message or raise NotFound."""
    msg = await _MessageDoc.get(PydanticObjectId(message_id))
    if not msg or msg.deleted or msg.context_type != "group" or not msg.group:
        raise NotFound("message", message_id)
    return msg


async def _get_group_message_domain_or_404(message_id: str) -> _MessageDomain:
    """Same as ``_get_group_message_or_404`` but returns the domain message."""
    msg = await _message_get_domain(message_id)
    if not msg or msg.deleted or msg.context_type != "group" or not msg.group:
        raise NotFound("message", message_id)
    return msg


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


async def send_message(group_id: str, user_id: str, body: SendMessageRequest) -> dict:
    """Send a message to a group.

    Verifies membership, checks group is not archived, creates the
    Message document, emits a ``message.sent`` event, and updates the
    group's last_message_at / message_count.
    """
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    group = await _get_group_or_404(group_id)
    _require_can_post(group, user_id)

    # Enforce per-member posting restrictions
    member_role = group_service._role_for(group, user_id)
    if member_role == "post_no_media" and body.attachments:
        raise Forbidden(
            "group.attachments_disabled",
            "Your role does not allow sending file attachments",
        )

    if group.archived:
        raise Forbidden("group.archived", "Cannot send messages to an archived group")

    domain_msg = await _create_group_message_doc(
        group_id=group_id,
        sender=user_id,
        sender_type="user",
        content=body.content,
        mentions=body.mentions,
        attachments=body.attachments,
        reply_to=body.reply_to,
        thread_id=body.thread_id,
    )

    bumped_at = domain_msg.created_at or datetime.now(UTC)
    await group_service.bump_message_stats(group_id, last_message_at=bumped_at)

    parent_domain = await _message_get_domain(body.reply_to) if body.reply_to else None
    if parent_domain is not None and parent_domain.context_type != "group":
        parent_domain = None

    response = message_to_wire_dict(domain_msg, parent=parent_domain)

    reply_meta: dict = {}
    if body.reply_to:
        reply_meta["reply_to"] = body.reply_to
        if parent_domain is not None:
            reply_meta["reply_to_sender_type"] = parent_domain.sender_type
            reply_meta["reply_to_agent_id"] = parent_domain.agent

    await event_bus.emit(
        "message.sent",
        {
            "group_id": group_id,
            "message_id": domain_msg.id,
            "sender_id": user_id,
            "sender_type": "user",
            "content": body.content,
            "mentions": body.mentions,
            "attachments": body.attachments,
            "workspace_id": group.workspace,
            **reply_meta,
        },
    )

    await emit(MessageNew(data={**response, "group_id": group_id}))
    await emit(MessageSent(data={**response, "group_id": group_id, "sender_id": user_id}))

    unread_tasks = [
        emit(UnreadUpdate(data={"group_id": group_id, "user_id": member, "delta": 1}))
        for member in group.members
        if member != user_id
    ]
    if unread_tasks:
        await asyncio.gather(*unread_tasks)

    group_name = getattr(group, "name", "") or ""

    # --- In-app notification: create for DM and group messages ---
    group_type = getattr(group, "type", "")
    is_dm = group_type == "dm"

    # Only create notifications for non-self messages
    notif_recipients = [m for m in group.members if m != user_id]
    if notif_recipients:
        try:
            sender_doc = await _UserDoc.get(PydanticObjectId(user_id))
            sender_name = sender_doc.full_name or sender_doc.email or "Someone"
        except (ValueError, AttributeError):
            sender_name = "Someone"

        title = (
            f"New message from {sender_name}"
            if is_dm
            else f"New message in #{group_name}"
            if group_name
            else "New message"
        )

        notif_tasks = [
            notifications_service.create(
                workspace_id=str(group.workspace),
                recipient=member,
                kind="message",
                title=title,
                body=body.content[:200],
                source=NotificationSource(
                    type="message",
                    id=domain_msg.id,
                    pocket_id=None,
                    room_id=group_id,
                ),
            )
            for member in notif_recipients
        ]
        await asyncio.gather(*notif_tasks)
    broadcast_types = {"here", "channel", "everyone"}
    recipients: set[str] = set()

    for mention in body.mentions or []:
        if not isinstance(mention, dict):
            continue
        mtype = mention.get("type")
        if mtype == "user":
            target = mention.get("id")
            if target and target != user_id:
                recipients.add(target)
        elif mtype in broadcast_types:
            for member in group.members:
                if member != user_id:
                    recipients.add(member)

    async def _fan_out_mention(target: str) -> None:
        await notifications_service.create(
            workspace_id=str(group.workspace),
            recipient=target,
            kind="mention",
            title=(f"You were mentioned in #{group_name}" if group_name else "You were mentioned"),
            body=body.content[:200],
            source=NotificationSource(
                type="message",
                id=domain_msg.id,
                pocket_id=None,
                room_id=group_id,
            ),
        )
        await unread_service.bump_mention(target, group_id)

    if recipients:
        await asyncio.gather(*(_fan_out_mention(t) for t in recipients))

    return response


async def create_agent_message(
    group_id: str,
    agent_id: str,
    content: str,
    attachments: list[_AttachmentDoc] | None = None,
) -> _MessageDoc:
    """Create a message from an agent in a group.

    Used by agent_bridge to persist agent responses. Returns the
    persisted Beanie Message document for legacy callers.
    """
    attachment_dicts = (
        [a.model_dump() if hasattr(a, "model_dump") else dict(a) for a in attachments or []]
        if attachments
        else None
    )
    domain_msg = await _create_group_message_doc(
        group_id=group_id,
        sender=None,
        sender_type="agent",
        agent=agent_id,
        content=content,
        attachments=attachment_dicts,
    )
    bumped_at = domain_msg.created_at or datetime.now(UTC)
    await group_service.bump_message_stats(group_id, last_message_at=bumped_at)
    return await _MessageDoc.get(PydanticObjectId(domain_msg.id))


async def edit_message(message_id: str, user_id: str, body: EditMessageRequest) -> dict:
    """Edit a message. Author only, and the author must still be able to post."""
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    msg = await _get_group_message_domain_or_404(message_id)

    if msg.sender != user_id:
        raise Forbidden("message.not_author", "Only the message author can edit it")

    group = await _get_group_or_404(cast(str, msg.group))
    _require_can_post(group, user_id)

    edited_at = datetime.now(UTC)
    domain_msg = await _edit_message_content(message_id, body.content, edited_at=edited_at)

    await emit(
        MessageEdited(
            data={
                "message_id": domain_msg.id,
                "group_id": cast(str, domain_msg.group),
                "content": domain_msg.content,
                "edited_at": str(domain_msg.edited_at),
            }
        )
    )
    return message_to_wire_dict(domain_msg)


async def delete_message(message_id: str, user_id: str) -> None:
    """Soft-delete a message. Author or group owner can delete."""
    msg = await _get_group_message_domain_or_404(message_id)

    if msg.sender != user_id:
        group = await _get_group_or_404(cast(str, msg.group))
        if group.owner != user_id:
            raise Forbidden(
                "message.not_authorized",
                "Only the author or group owner can delete this message",
            )

    await _soft_delete_message(message_id)

    await emit(
        MessageDeleted(
            data={
                "message_id": msg.id,
                "group_id": cast(str, msg.group),
            }
        )
    )


async def toggle_reaction(message_id: str, user_id: str, emoji: str) -> dict:
    """Toggle a reaction on a message."""
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    msg = await _get_group_message_domain_or_404(message_id)

    group = await _get_group_or_404(cast(str, msg.group))
    _require_can_post(group, user_id)

    domain_msg, added = await _toggle_reaction_doc(message_id, user_id, emoji)

    await emit(
        MessageReaction(
            data={
                "message_id": domain_msg.id,
                "group_id": cast(str, domain_msg.group),
                "emoji": emoji,
                "user_id": user_id,
            }
        )
    )

    if added and domain_msg.sender and domain_msg.sender != user_id:
        await notifications_service.create(
            workspace_id=str(group.workspace),
            recipient=domain_msg.sender,
            kind="reaction",
            title=f"{emoji} on your message",
            body=(domain_msg.content or "")[:200],
            source=NotificationSource(
                type="message",
                id=domain_msg.id,
                room_id=cast(str, domain_msg.group),
            ),
        )

    return message_to_wire_dict(domain_msg)


async def _resolve_session_for_message(session_key: str):
    """Find the Session row that owns a Message's ``session_key``.

    Two formats coexist for ``Message.session_key``:

      * Cloud-agent SSE flow writes the composite
        ``"cloud:session:<session._id>:<agent_id>"`` so memory entries are
        namespaced per (session, agent). The Session document stores its own
        ``sessionId`` (a UUID-like string), not this composite — a direct
        ``sessionId == session_key`` lookup misses.
      * Bus / memory paths write ``Session.sessionId`` straight (e.g.
        ``"websocket_abc123"`` or ``"telegram_42"``).

    Try the direct match first (cheap, covers the bus path), then fall back to
    parsing the composite and resolving by Mongo ``_id``. Returns ``None`` if
    neither hits.
    """
    from pocketpaw_ee.cloud.models.session import Session as _SessionDoc

    direct = await _SessionDoc.find_one(_SessionDoc.sessionId == session_key)
    if direct is not None:
        return direct

    if session_key.startswith("cloud:session:"):
        parts = session_key.split(":")
        # Expected shape: ["cloud", "session", "<oid>", "<agent>"]; agent is
        # optional historically but the oid slot is always present.
        if len(parts) >= 3 and parts[2]:
            try:
                oid = PydanticObjectId(parts[2])
            except Exception:
                return None
            return await _SessionDoc.get(oid)

    return None


async def patch_ui_state(
    message_id: str,
    user_id: str,
    spec_id: str,
    state: dict,
) -> dict:
    """Persist Ripple inline-UI state by splicing it into ``message.content``.

    Why content-mutation rather than a side field: when the agent later reads
    chat history for context it sees ``message.content`` directly, so the
    ui-spec JSON inside must reflect the user's interactions — otherwise the
    model's memory is permanently stuck on the original cards. Storing state
    on a sibling ``ui_state`` field worked for rendering but lied to the
    agent. See ``ripple_content_patcher`` for the splice logic.

    Authz follows the message's context:
      * ``group``  — caller must be a group member.
      * ``pocket`` / ``session`` — caller must own the linked Session.

    Last-write-wins on the entire ``spec_id`` (no field-level merge);
    Ripple's ``onStateChange`` always carries the full state snapshot.

    Emits ``message.ui_state.updated`` with routing keys (``group_id`` for
    group messages, ``user_id`` for pocket/session messages) so other tabs /
    members of the room re-fetch / re-render content via the WS bridge.
    """
    from pocketpaw_ee.cloud.chat.ripple_content_patcher import patch_content_with_state

    # Treat malformed ids (e.g. ``m<timestamp>`` placeholders the FE assigns
    # before the cloud ObjectId arrives) as 404 — InvalidId would otherwise
    # bubble up to a 500.
    try:
        oid = PydanticObjectId(message_id)
    except Exception as exc:
        raise NotFound("message", message_id) from exc

    msg = await _MessageDoc.get(oid)
    if msg is None or msg.deleted:
        raise NotFound("message", message_id)

    ctx = msg.context_type or "group"
    if ctx == "group":
        if not msg.group:
            raise NotFound("message", message_id)
        group = await _get_group_or_404(msg.group)
        _require_group_member(group, user_id)
        route_key: dict = {"group_id": msg.group}
    elif ctx in ("pocket", "session"):
        if not msg.session_key:
            raise NotFound("message", message_id)
        sess = await _resolve_session_for_message(msg.session_key)
        if sess is None:
            raise NotFound("session", msg.session_key)
        if sess.owner != user_id:
            raise Forbidden(
                "message.not_authorized",
                "Only the session owner can update inline UI state",
            )
        route_key = {"user_id": user_id}
    else:
        raise NotFound("message", message_id)

    new_content = patch_content_with_state(msg.content or "", spec_id, state)
    if new_content is None:
        # Fence not found / malformed JSON — surface a 404 so the FE can
        # fall back to in-memory state rather than silently dropping the
        # patch. (A 500 would be wrong: nothing crashed, the target just
        # doesn't exist in the document we have.)
        raise NotFound("ui_spec", f"{message_id}/{spec_id}")

    msg.content = new_content
    await msg.save()

    await emit(
        MessageUiStateUpdated(
            data={
                "message_id": message_id,
                "spec_id": spec_id,
                "state": state,
                "content": new_content,
                **route_key,
            }
        )
    )
    return {"ok": True}


async def get_messages(
    group_id: str,
    user_id: str,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """Cursor-based paginated messages, newest first.

    Cursor format: ``"{iso_timestamp}|{object_id}"``. Fetches ``limit + 1``
    to determine ``has_more``. The response includes ``active_run`` (newest
    non-terminal ``ChatRunDoc`` for this group across both ``dm`` and
    ``group`` context_types) for frontend auto-resume.
    """
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    group = await _get_group_or_404(group_id)
    if group.type in ("private", "dm"):
        _require_group_member(group, user_id)

    before_time: datetime | None = None
    before_id: str | None = None
    if cursor:
        parts = cursor.split("|", 1)
        if len(parts) == 2:
            try:
                before_time = datetime.fromisoformat(parts[0])
                before_id = parts[1]
            except ValueError:
                before_time = None
                before_id = None

    messages = await _list_for_group_paged(
        group_id,
        before_time=before_time,
        before_id=before_id,
        limit=limit + 1,
    )
    has_more = len(messages) > limit
    if has_more:
        messages = messages[:limit]

    parent_ids = [m.reply_to for m in messages if m.reply_to]
    parent_domains = await _message_get_many_domain(parent_ids) if parent_ids else []
    parents_by_id = {p.id: p for p in parent_domains}

    items = [
        message_to_wire_dict(m, parent=parents_by_id.get(m.reply_to) if m.reply_to else None)
        for m in messages
    ]

    next_cursor: str | None = None
    if has_more and messages:
        last = messages[-1]
        if last.created_at is not None:
            next_cursor = f"{last.created_at.isoformat()}|{last.id}"

    active = await run_service.find_active_run_for_scope(
        workspace_id=group.workspace,
        context_type=("dm", "group"),
        scope_id=group_id,
    )
    active_run = {"run_id": active.run_id, "status": active.status} if active else None

    return {
        "items": items,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "active_run": active_run,
    }


async def get_thread(message_id: str, user_id: str) -> list[dict]:
    """Get all replies to a message, sorted ascending by creation time."""
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    msg = await _get_group_message_domain_or_404(message_id)
    group = await _get_group_or_404(cast(str, msg.group))
    if group.type in ("private", "dm"):
        _require_group_member(group, user_id)

    replies = await _list_replies(msg.id)
    return [message_to_wire_dict(r) for r in replies]


# ---------------------------------------------------------------------------
# Thread operations
# ---------------------------------------------------------------------------


async def create_thread(group_id: str, user_id: str, message_id: str) -> dict:
    """Create a thread from a message.

    Marks the message as a thread parent and adds the message id to the
    group's active_threads list. Returns the parent message dict.
    """
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    group = await _get_group_or_404(group_id)
    _require_can_post(group, user_id)

    msg = await _MessageDoc.get(PydanticObjectId(message_id))
    if not msg or msg.deleted or msg.context_type != "group" or msg.group != group_id:
        raise NotFound("message", message_id)

    # Mark the message as a thread parent
    if not msg.is_thread_parent:
        msg.is_thread_parent = True
        # Bump thread_count to indicate it has replies (even if 0 yet)
        await msg.save()

    # Add to the group's active_threads list if not already there
    if message_id not in group.active_threads:
        group.active_threads.append(message_id)
        await group.save()

    domain_msg = _message_doc_to_domain(msg)
    wire = message_to_wire_dict(domain_msg)

    await emit(
        ThreadReply(
            data={
                "type": "thread.created",
                "group_id": group_id,
                "message_id": message_id,
                "message": wire,
            }
        )
    )

    return wire


async def get_active_threads(group_id: str, user_id: str) -> list[dict]:
    """List active (non-closed) threads in a group.

    Returns thread parent messages ordered by most recent thread activity.
    Includes a ``replyCount`` and ``lastReplyAt`` field on each item.
    """
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    group = await _get_group_or_404(group_id)
    if group.type in ("private", "dm"):
        _require_group_member(group, user_id)

    if not group.active_threads:
        return []

    # Fetch all thread parent messages
    oids: list[PydanticObjectId] = []
    for tid in group.active_threads:
        try:
            oids.append(PydanticObjectId(tid))
        except Exception:
            continue

    if not oids:
        return []

    parent_docs = await _MessageDoc.find({"_id": {"$in": oids}, "deleted": False}).to_list()
    parent_by_id = {str(p.id): p for p in parent_docs}

    active_thread_ids = [str(p.id) for p in parent_docs]
    if not active_thread_ids:
        return []

    # Single aggregation to get reply_count and last_reply_at for all active threads
    pipeline = [
        {
            "$match": {
                "context_type": "group",
                "thread_id": {"$in": active_thread_ids},
                "deleted": False,
            }
        },
        {
            "$group": {
                "_id": "$thread_id",
                "reply_count": {"$sum": 1},
                "last_reply_at": {"$max": "$createdAt"},
            }
        },
    ]
    agg_cursor = _MessageDoc.aggregate(pipeline)
    thread_stats: dict[str, dict] = {}
    async for doc in agg_cursor:
        tid = doc["_id"]
        thread_stats[tid] = {
            "reply_count": doc["reply_count"],
            "last_reply_at": doc.get("last_reply_at"),
        }

    # Build response using the aggregated stats
    result = []
    for tid in active_thread_ids:
        parent = parent_by_id[tid]
        stats = thread_stats.get(tid, {})
        reply_count = stats.get("reply_count", 0)
        last_reply_at = stats.get("last_reply_at") or parent.createdAt

        domain_msg = _message_doc_to_domain(parent)
        wire = message_to_wire_dict(domain_msg)
        wire["replyCount"] = reply_count
        wire["lastReplyAt"] = iso_utc(last_reply_at)
        result.append(wire)

    # Sort by most recent reply first
    result.sort(key=lambda x: x.get("lastReplyAt", "") or "", reverse=True)
    return result


async def close_thread(group_id: str, user_id: str, thread_id: str) -> None:
    """Close/archive a thread.

    Removes the thread parent message id from the group's active_threads.
    The thread messages remain but the thread no longer appears in the
    active threads list.
    """
    group = await _get_group_or_404(group_id)

    # Only group admins/owner or thread author can close
    if group.owner != user_id and group.member_roles.get(user_id) != "admin":
        msg = await _MessageDoc.get(PydanticObjectId(thread_id))
        if not msg or msg.deleted or str(msg.group) != group_id:
            raise NotFound("thread", thread_id)
        if msg.sender != user_id:
            raise Forbidden(
                "thread.not_authorized",
                "Only group admins or the thread author can close a thread",
            )

    if thread_id not in group.active_threads:
        raise NotFound("thread", thread_id)

    group.active_threads.remove(thread_id)
    await group.save()

    await emit(
        ThreadReply(
            data={
                "type": "thread.closed",
                "group_id": group_id,
                "thread_id": thread_id,
            }
        )
    )


async def get_thread_messages(
    thread_id: str,
    user_id: str,
    *,
    group_id: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict:
    """Get all messages in a thread, paginated oldest-first.

    Includes the parent message as the first item.

    If *group_id* is provided it is validated against the parent message's
    stored group to prevent cross-group access via the URL parameter.
    """
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    # Verify the thread parent exists and user can access
    parent = await _message_get_domain(thread_id)
    if not parent or parent.deleted or parent.context_type != "group" or not parent.group:
        raise NotFound("thread", thread_id)

    # If caller supplied a group_id URL param, assert it matches the
    # parent message's actual group — prevents cross-group access.
    if group_id is not None and str(parent.group) != group_id:
        raise NotFound("thread", thread_id)

    group = await _get_group_or_404(parent.group)
    if group.type in ("private", "dm"):
        _require_group_member(group, user_id)

    # Build response: parent + thread replies
    before_time: datetime | None = None
    before_id: str | None = None
    if cursor:
        parts = cursor.split("|", 1)
        if len(parts) == 2:
            try:
                before_time = datetime.fromisoformat(parts[0])
                before_id = parts[1]
            except ValueError:
                before_time = None
                before_id = None

    # Fetch thread replies with cursor pagination (oldest-first after parent)
    query: dict = {
        "context_type": "group",
        "thread_id": thread_id,
        "deleted": False,
    }
    if before_time is not None and before_id is not None:
        try:
            cursor_oid = PydanticObjectId(before_id)
        except Exception:
            cursor_oid = None
        if cursor_oid is not None:
            query["$or"] = [
                {"createdAt": {"$gt": before_time}},
                {"createdAt": before_time, "_id": {"$gt": cursor_oid}},
            ]

    replies = (
        await _MessageDoc.find(query)
        .sort([("createdAt", 1), ("_id", 1)])
        .limit(limit + 1)
        .to_list()
    )

    has_more = len(replies) > limit
    if has_more:
        replies = replies[:limit]

    items = [message_to_wire_dict(parent)]
    items.extend(message_to_wire_dict(_message_doc_to_domain(r)) for r in replies)

    next_cursor: str | None = None
    if has_more and replies:
        last = replies[-1]
        if last.createdAt:
            next_cursor = f"{last.createdAt.isoformat()}|{str(last.id)}"

    return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}


async def pin_message(group_id: str, user_id: str, message_id: str) -> None:
    """Pin a message in a group. Owner only."""
    group = await group_service._get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)

    msg = await _message_get_domain(message_id)
    if msg is None or msg.group != group_id:
        raise NotFound("message", message_id)

    await group_service._pin_message_doc(group_id, message_id)


async def unpin_message(group_id: str, user_id: str, message_id: str) -> None:
    """Unpin a message from a group. Owner only."""
    group = await group_service._get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)

    result = await group_service._unpin_message_doc(group_id, message_id)
    if result is None:
        raise NotFound("pinned_message", message_id)


async def search_messages(group_id: str, user_id: str, query: str) -> list[dict]:
    """Search messages by content using regex. Limited to 50 results."""
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    group = await _get_group_or_404(group_id)
    if group.type in ("private", "dm"):
        _require_group_member(group, user_id)

    domain_messages = await _search_in_group(group_id, query, limit=50)
    return [message_to_wire_dict(m) for m in domain_messages]


async def search_workspace_messages(
    workspace_id: str,
    user_id: str,
    query: str,
    limit: int = 50,
) -> list[dict]:
    """Full-text-ish search across the workspace, honoring per-group scope.

    The caller sees hits only from public/channel groups in
    ``workspace_id`` plus private/dm groups where the caller is an
    explicit member. Capped at 100 results.
    """
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    capped = max(1, min(limit, 100))
    if not query or not query.strip():
        return []

    groups = await group_service._list_visible_in_workspace(workspace_id, user_id)
    group_ids = [g.id for g in groups]
    if not group_ids:
        return []

    messages = await _search_in_groups(group_ids, query.strip(), limit=capped)
    return [message_to_wire_dict(m) for m in messages]


async def persist_pocket_memory_message(
    *,
    session_key: str,
    role: str,
    sender_type: str,
    content: str,
    workspace_id: str | None,
    attachments: list[dict] | None = None,
) -> str:
    """Insert a pocket-context Message used by the cloud memory store.

    Returns the new message id (as str). Used by ``MongoMemoryStore.save``
    so the memory adapter doesn't import the Message Beanie class.
    """
    attachment_docs = [_AttachmentDoc(**a) for a in attachments] if attachments else []
    msg = _MessageDoc(
        context_type="pocket",
        session_key=session_key,
        role=role,  # type: ignore[arg-type]
        sender_type=sender_type,
        content=content,
        workspace_id=workspace_id,
        attachments=attachment_docs,
    )
    await msg.insert()
    return str(msg.id)


async def find_pocket_dedup_twin_id(
    session_key: str, role: str, content: str, *, window_seconds: int = 5
) -> str | None:
    """Return the id of an existing pocket-context Message that matches
    ``(session_key, role, content)`` within the dedup window, else ``None``.

    Used by the memory store to drop duplicate writes on the synchronous
    in-request race between the chat endpoint and the agent loop's
    ``memory.add_to_session`` calling here with the same content.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    try:
        doc = await _MessageDoc.find_one(
            {
                "context_type": "pocket",
                "session_key": session_key,
                "role": role,
                "content": content,
                "createdAt": {"$gte": cutoff},
            }
        )
    except Exception:
        logger.exception("memory dedup lookup failed for session=%s", session_key)
        return None
    return str(doc.id) if doc else None


async def delete_message_doc_by_id(message_id: str) -> bool:
    """Hard-delete a Message Beanie row by id. Returns True if a row was
    removed. Used by the memory store's ``delete`` and ``clear_session``."""
    try:
        oid = PydanticObjectId(message_id)
    except Exception:
        return False
    doc = await _MessageDoc.get(oid)
    if doc is None:
        return False
    await doc.delete()
    return True


async def list_recent_for_group(group_id: str, *, limit: int = 20) -> list[_MessageDomain]:
    """Return up to ``limit`` most-recent non-deleted group messages,
    oldest-first. Used by the agent bridge to rehydrate prior turns
    before a ``pool.run`` call."""
    docs = (
        await _MessageDoc.find(
            _MessageDoc.group == group_id,
            _MessageDoc.deleted == False,  # noqa: E712
        )
        .sort(-_MessageDoc.createdAt)
        .limit(limit)
        .to_list()
    )
    docs.reverse()
    return [_message_doc_to_domain(d) for d in docs]


# Agent-stream persist helpers — bypass ``send_message`` (and its
# ``message.sent`` emit that would re-trigger ``agent_bridge``) since the
# run executor is the sole driver of the reply.


async def persist_user_message_for_scope(
    *,
    kind: str,
    scope_id: str,
    user_id: str,
    workspace_id: str,
    session_key: str,
    content: str,
    attachments: list[dict] | None = None,
    mentions: list[dict] | None = None,
    reply_to: str | None = None,
) -> str:
    """Persist the caller's message in an agent-stream context."""
    if kind in ("pocket", "session"):
        msg = _MessageDoc(
            context_type=kind,  # type: ignore[arg-type]
            session_key=session_key,
            role="user",
            sender=user_id,
            sender_type="user",
            content=content,
            attachments=attachments or [],
            workspace_id=workspace_id,
        )
    else:
        msg = _MessageDoc(
            context_type="group",
            group=scope_id,
            sender=user_id,
            sender_type="user",
            content=content,
            attachments=attachments or [],
            mentions=mentions or [],
            reply_to=reply_to,
            workspace_id=workspace_id,
        )
    await msg.insert()
    return str(msg.id)


async def persist_assistant_message_for_scope(
    *,
    kind: str,
    scope_id: str,
    user_id: str,
    workspace_id: str,
    session_key: str,
    target_agent_id: str,
    content: str,
    attachments: list[dict] | None = None,
) -> _MessageDoc:
    """Persist an agent's reply in an agent-stream context."""
    att_models = [_AttachmentDoc(**a) if isinstance(a, dict) else a for a in (attachments or [])]
    if kind in ("pocket", "session"):
        msg = _MessageDoc(
            context_type=kind,  # type: ignore[arg-type]
            session_key=session_key,
            role="assistant",
            sender=None,
            sender_type="agent",
            agent=target_agent_id,
            content=content,
            attachments=att_models,
            workspace_id=workspace_id,
        )
    else:
        msg = _MessageDoc(
            context_type="group",
            group=scope_id,
            sender=None,
            sender_type="agent",
            agent=target_agent_id,
            content=content,
            attachments=att_models,
            workspace_id=workspace_id,
        )
    await msg.insert()
    return msg


__all__ = [
    "close_thread",
    "create_agent_message",
    "create_thread",
    "delete_message",
    "delete_message_doc_by_id",
    "edit_message",
    "find_pocket_dedup_twin_id",
    "get_active_threads",
    "get_messages",
    "get_thread",
    "get_thread_messages",
    "list_recent_for_group",
    "patch_ui_state",
    "persist_assistant_message_for_scope",
    "persist_pocket_memory_message",
    "persist_user_message_for_scope",
    "pin_message",
    "search_messages",
    "search_workspace_messages",
    "send_message",
    "toggle_reaction",
    "unpin_message",
]
