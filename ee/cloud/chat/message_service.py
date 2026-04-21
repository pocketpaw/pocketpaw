# Refactored: Split from service.py — contains MessageService class and message-related
# helper functions. Added create_agent_message() static method for use by agent_bridge
# instead of creating Message documents directly.
# 2026-04-19: ``message.sent`` event now carries ``attachments`` so agent_bridge
# can surface filename/mime/size into the channel agent prompt (fixes silent
# attachment drop on the channel path — DM path already had this).
# 2026-04-19 (Cluster E sub-PR 2): added `search_workspace_messages` which
# scopes to groups the caller is a member of and to public/channel rooms in
# the workspace, escapes the query with re.escape, and caps at 100 hits.

"""Chat domain — message business logic (CRUD, reactions, threads, pins, search)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import cast

from beanie import PydanticObjectId

from ee.cloud.chat.group_service import (
    _get_group_or_404,
    _require_can_post,
    _require_group_admin,
    _require_group_member,
)
from ee.cloud.chat.schemas import (
    EditMessageRequest,
    SendMessageRequest,
)
from ee.cloud.chat.unread_service import UnreadService
from ee.cloud.models.message import Attachment, Mention, Message, Reaction
from ee.cloud.models.notification import NotificationSource
from ee.cloud.notifications.service import NotificationService
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    MessageDeleted,
    MessageEdited,
    MessageNew,
    MessageReaction,
    MessageSent,
    UnreadUpdate,
)
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_REPLY_PREVIEW_CHARS = 140


def _reply_preview(parent: Message | None) -> dict | None:
    """Build a small preview payload for an inline reply quote.

    Rendered as the small quote bubble above a reply message. We keep the
    text short so history payloads don't balloon for channels with lots
    of replies. ``None`` when the parent is missing or soft-deleted — the
    FE falls back to a "message deleted" placeholder.
    """
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


def _message_response(msg: Message, *, parent: Message | None = None) -> dict:
    """Convert a Message document to a frontend-compatible dict.

    When ``parent`` is supplied (because the caller already fetched it,
    or prefetched a batch for a list view), a ``replyPreview`` field is
    included so the FE can render the inline quote without a second
    fetch. For replies where the parent isn't resolvable (deleted, or
    caller chose not to fetch), ``replyPreview`` is ``None``.
    """
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
        "attachments": [a.model_dump() for a in msg.attachments],
        "reactions": [r.model_dump() for r in msg.reactions],
        "edited": msg.edited,
        "editedAt": iso_utc(msg.edited_at),
        "deleted": msg.deleted,
        "createdAt": iso_utc(msg.createdAt),
    }


async def _get_group_message_or_404(message_id: str) -> Message:
    """Load a non-deleted group-context message or raise NotFound.

    Pocket-context messages are not addressable via the group chat routes —
    if the id resolves to a pocket row it's treated as not found.
    """
    msg = await Message.get(PydanticObjectId(message_id))
    if not msg or msg.deleted or msg.context_type != "group" or not msg.group:
        raise NotFound("message", message_id)
    return msg


# ---------------------------------------------------------------------------
# MessageService
# ---------------------------------------------------------------------------


class MessageService:
    """Stateless service for message business logic."""

    @staticmethod
    async def send_message(group_id: str, user_id: str, body: SendMessageRequest) -> dict:
        """Send a message to a group.

        Verifies membership, checks group is not archived, creates the
        Message document, emits a ``message.sent`` event, and updates
        the group's last_message_at / message_count.
        """
        group = await _get_group_or_404(group_id)
        _require_can_post(group, user_id)

        if group.archived:
            raise Forbidden("group.archived", "Cannot send messages to an archived group")

        mentions = [Mention(**m) for m in body.mentions]
        attachments = [Attachment(**a) for a in body.attachments]

        msg = Message(
            context_type="group",
            group=group_id,
            sender=user_id,
            sender_type="user",
            content=body.content,
            mentions=mentions,
            reply_to=body.reply_to,
            attachments=attachments,
        )
        await msg.insert()

        # Update group stats
        group.last_message_at = msg.createdAt
        group.message_count += 1
        await group.save()

        # Resolve the reply parent once so we can both embed a preview in
        # the response and keep replies rendering inline in the main feed.
        # The previous implementation also bumped ``parent.thread_count``
        # and emitted a ``ThreadReply`` event — we've moved to inline
        # quoted replies (Telegram-style), so neither is needed: the
        # parent's counter isn't displayed anywhere and the reply fans out
        # via ``MessageNew`` like any other message.
        parent: Message | None = None
        if body.reply_to:
            try:
                parent_id = PydanticObjectId(body.reply_to)
                parent = await Message.find_one({"_id": parent_id, "context_type": "group"})
            except Exception:
                # Bad reply_to — the reply still sends; FE will render it
                # without a quote bubble.
                parent = None

        response = _message_response(msg, parent=parent)

        # Thread reply-target metadata into the event so the agent bridge can
        # decide whether an ``auto``-mode agent should respond. A reply aimed
        # at a human is a directed side-conversation and shouldn't summon
        # auto agents; a reply aimed at an agent is a follow-up and should
        # go through the normal respond-mode logic.
        reply_meta: dict = {}
        if body.reply_to:
            reply_meta["reply_to"] = body.reply_to
            if parent is not None:
                reply_meta["reply_to_sender_type"] = parent.sender_type
                reply_meta["reply_to_agent_id"] = parent.agent

        await event_bus.emit(
            "message.sent",
            {
                "group_id": group_id,
                "message_id": str(msg.id),
                "sender_id": user_id,
                "sender_type": "user",
                "content": body.content,
                "mentions": body.mentions,
                # Attachments ride on the event so ``agent_bridge`` can inject
                # filename / mime / size into the agent prompt. Mirrors the DM
                # path's file-awareness contract; raw dicts match
                # ``body.attachments``'s shape and downstream handlers no-op on
                # unknown keys.
                "attachments": body.attachments,
                "workspace_id": group.workspace,
                **reply_meta,
            },
        )

        # Realtime fan-out: message.new to the group (sender excluded via
        # AudienceResolver reading data["sender"]), message.sent ack to the
        # sender only (keyed by data["sender_id"]). Inline replies share
        # the same ``MessageNew`` channel as top-level messages — the quote
        # bubble is rendered client-side from ``replyPreview``.
        await emit(MessageNew(data={**response, "group_id": group_id}))
        await emit(MessageSent(data={**response, "group_id": group_id, "sender_id": user_id}))

        # Unread badge sync — every non-sender member receives a delta so their
        # client can increment the sidebar counter without a full /unreads refetch.
        # Concurrent emit() so a 1000-member channel isn't 999 sequential awaits
        # on the sender's request path.
        unread_tasks = [
            emit(UnreadUpdate(data={"group_id": group_id, "user_id": member, "delta": 1}))
            for member in group.members
            if member != user_id
        ]
        if unread_tasks:
            await asyncio.gather(*unread_tasks)

        # Derive mention notifications. Dedupe recipients across multiple
        # mentions in the same message. Broadcast types (@here/@channel/@everyone)
        # target every non-sender member. User mentions add the specific user.
        group_name = getattr(group, "name", "") or ""
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
            # "agent" and "channel_ref" skip — not a user notification trigger.

        async def _fan_out_mention(target: str) -> None:
            await NotificationService.create(
                workspace_id=str(group.workspace),
                recipient=target,
                kind="mention",
                title=f"You were mentioned in #{group_name}" if group_name else "You were mentioned",
                body=body.content[:200],
                source=NotificationSource(
                    type="message",
                    id=str(msg.id),
                    pocket_id=None,
                ),
            )
            await UnreadService.bump_mention(target, group_id)

        if recipients:
            await asyncio.gather(*(_fan_out_mention(t) for t in recipients))

        return response

    @staticmethod
    async def create_agent_message(
        group_id: str,
        agent_id: str,
        content: str,
        attachments: list[Attachment] | None = None,
    ) -> Message:
        """Create a message from an agent in a group.

        Used by agent_bridge to persist agent responses instead of creating
        Message documents directly. Returns the persisted Message document.
        """
        msg = Message(
            context_type="group",
            group=group_id,
            sender=None,
            sender_type="agent",
            agent=agent_id,
            content=content,
            attachments=attachments or [],
        )
        await msg.insert()

        # Update group stats
        group = await _get_group_or_404(group_id)
        group.last_message_at = msg.createdAt
        group.message_count += 1
        await group.save()

        return msg

    @staticmethod
    async def edit_message(message_id: str, user_id: str, body: EditMessageRequest) -> dict:
        """Edit a message. Author only, and the author must still be able to post."""
        msg = await _get_group_message_or_404(message_id)

        if msg.sender != user_id:
            raise Forbidden("message.not_author", "Only the message author can edit it")

        # Defense-in-depth: if the author's role has been downgraded to view,
        # block edits even though they authored the message.
        group = await _get_group_or_404(cast(str, msg.group))
        _require_can_post(group, user_id)

        msg.content = body.content
        msg.edited = True
        msg.edited_at = datetime.now(UTC)
        await msg.save()

        await emit(
            MessageEdited(
                data={
                    "message_id": str(msg.id),
                    "group_id": cast(str, msg.group),
                    "content": msg.content,
                    "edited_at": str(msg.edited_at),
                }
            )
        )

        return _message_response(msg)

    @staticmethod
    async def delete_message(message_id: str, user_id: str) -> None:
        """Soft-delete a message. Author or group owner can delete."""
        msg = await _get_group_message_or_404(message_id)

        if msg.sender != user_id:
            # Check if user is the group owner
            group = await _get_group_or_404(cast(str, msg.group))
            if group.owner != user_id:
                raise Forbidden(
                    "message.not_authorized",
                    "Only the author or group owner can delete this message",
                )

        msg.deleted = True
        await msg.save()

        await emit(
            MessageDeleted(
                data={
                    "message_id": str(msg.id),
                    "group_id": cast(str, msg.group),
                }
            )
        )

    @staticmethod
    async def toggle_reaction(message_id: str, user_id: str, emoji: str) -> dict:
        """Toggle a reaction on a message.

        If the user already reacted with the given emoji, remove their
        reaction. Otherwise, add it. If the emoji reaction has no users
        left, remove the entire reaction entry.
        """
        msg = await _get_group_message_or_404(message_id)

        # View-only members cannot react
        group = await _get_group_or_404(cast(str, msg.group))
        _require_can_post(group, user_id)

        # Find existing reaction for this emoji
        existing: Reaction | None = None
        for r in msg.reactions:
            if r.emoji == emoji:
                existing = r
                break

        added = True
        if existing is not None:
            if user_id in existing.users:
                # Remove user from this reaction
                existing.users.remove(user_id)
                # Remove the reaction entry entirely if no users left
                if not existing.users:
                    msg.reactions.remove(existing)
                added = False
            else:
                existing.users.append(user_id)
        else:
            msg.reactions.append(Reaction(emoji=emoji, users=[user_id]))

        await msg.save()

        await emit(
            MessageReaction(
                data={
                    "message_id": str(msg.id),
                    "group_id": cast(str, msg.group),
                    "emoji": emoji,
                    "user_id": user_id,
                }
            )
        )

        # Derive reaction notification: only on ADD, and only if the reactor
        # is not the original sender of the message.
        if added and msg.sender and msg.sender != user_id:
            await NotificationService.create(
                workspace_id=str(group.workspace),
                recipient=msg.sender,
                kind="reaction",
                title=f"{emoji} on your message",
                body=(msg.content or "")[:200],
                source=NotificationSource(type="message", id=str(msg.id)),
            )

        return _message_response(msg)

    @staticmethod
    async def get_messages(
        group_id: str,
        user_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Cursor-based paginated messages, newest first.

        Cursor format: ``"{iso_timestamp}|{object_id}"``.
        Fetches ``limit + 1`` to determine ``has_more``.
        Excludes soft-deleted messages.
        """
        group = await _get_group_or_404(group_id)

        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        query: dict = {"context_type": "group", "group": group_id, "deleted": False}

        if cursor:
            parts = cursor.split("|", 1)
            if len(parts) == 2:
                cursor_time = datetime.fromisoformat(parts[0])
                cursor_id = PydanticObjectId(parts[1])
                query["$or"] = [
                    {"createdAt": {"$lt": cursor_time}},
                    {"createdAt": cursor_time, "_id": {"$lt": cursor_id}},
                ]

        messages = (
            await Message.find(query)
            .sort([("createdAt", -1), ("_id", -1)])
            .limit(limit + 1)
            .to_list()
        )

        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        # Batch-fetch parents for any replies in this page so ``replyPreview``
        # lands on each reply without an N+1 round-trip. Missing parents are
        # tolerated — the FE falls back to a "message unavailable" bubble.
        parent_ids = {m.reply_to for m in messages if m.reply_to}
        parents_by_id: dict[str, Message] = {}
        if parent_ids:
            try:
                oids = [PydanticObjectId(pid) for pid in parent_ids if pid]
            except Exception:
                oids = []
            if oids:
                parent_docs = await Message.find({"_id": {"$in": oids}}).to_list()
                parents_by_id = {str(p.id): p for p in parent_docs}

        items = [
            _message_response(m, parent=parents_by_id.get(m.reply_to) if m.reply_to else None)
            for m in messages
        ]

        next_cursor: str | None = None
        if has_more and messages:
            last = messages[-1]
            next_cursor = f"{last.createdAt.isoformat()}|{last.id}"

        return {"items": items, "nextCursor": next_cursor, "hasMore": has_more}

    @staticmethod
    async def get_thread(message_id: str, user_id: str) -> list[dict]:
        """Get all replies to a message, sorted ascending by creation time."""
        msg = await _get_group_message_or_404(message_id)

        # Verify user can access the group
        group = await _get_group_or_404(cast(str, msg.group))
        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        replies = (
            await Message.find({"context_type": "group", "reply_to": str(msg.id), "deleted": False})
            .sort([("createdAt", 1)])
            .to_list()
        )
        return [_message_response(r) for r in replies]

    @staticmethod
    async def pin_message(group_id: str, user_id: str, message_id: str) -> None:
        """Pin a message in a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        # Verify message belongs to this group
        msg = await _get_group_message_or_404(message_id)
        if msg.group != group_id:
            raise NotFound("message", message_id)

        if message_id not in group.pinned_messages:
            group.pinned_messages.append(message_id)
            await group.save()

    @staticmethod
    async def unpin_message(group_id: str, user_id: str, message_id: str) -> None:
        """Unpin a message from a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        if message_id not in group.pinned_messages:
            raise NotFound("pinned_message", message_id)

        group.pinned_messages.remove(message_id)
        await group.save()

    @staticmethod
    async def search_messages(group_id: str, user_id: str, query: str) -> list[dict]:
        """Search messages by content using regex. Limited to 50 results."""
        group = await _get_group_or_404(group_id)

        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        # Escape regex special characters for safe search
        escaped = re.escape(query)
        messages = (
            await Message.find(
                {
                    "context_type": "group",
                    "group": group_id,
                    "deleted": False,
                    "content": {"$regex": escaped, "$options": "i"},
                }
            )
            .sort([("createdAt", -1)])
            .limit(50)
            .to_list()
        )
        return [_message_response(m) for m in messages]

    @staticmethod
    async def search_workspace_messages(
        workspace_id: str,
        user_id: str,
        query: str,
        limit: int = 50,
    ) -> list[dict]:
        """Full-text-ish search across the workspace, honoring per-group scope.

        The caller sees hits only from:
          * public / channel groups in ``workspace_id`` (any workspace member
            can read these), plus
          * private / dm groups where the caller is an explicit member.

        Agent DMs are private groups with one member, so this also catches
        the user's own agent conversations.

        The query is escaped with ``re.escape`` before being fed to Mongo
        ``$regex`` so operators like ``.``, ``$``, and ``(`` become literal
        characters — no injection, no ReDoS via pathological expressions.
        Capped at 100 results to stop a wide query from paging the whole
        journal into memory.
        """
        from ee.cloud.models.group import Group

        capped = max(1, min(limit, 100))
        if not query or not query.strip():
            return []

        # 1. Resolve the set of group ids the caller is allowed to read in
        #    this workspace. One small index-friendly query, not N per hit.
        groups = await Group.find(
            {
                "workspace": workspace_id,
                "archived": False,
                "$or": [
                    {"type": {"$in": ["public", "channel"]}},
                    {"members": user_id},
                ],
            }
        ).to_list()
        group_ids = [str(g.id) for g in groups]
        if not group_ids:
            return []

        # 2. Escape the query + run the scoped regex search.
        escaped = re.escape(query.strip())
        messages = (
            await Message.find(
                {
                    "context_type": "group",
                    "group": {"$in": group_ids},
                    "deleted": False,
                    "content": {"$regex": escaped, "$options": "i"},
                }
            )
            .sort([("createdAt", -1)])
            .limit(capped)
            .to_list()
        )
        return [_message_response(m) for m in messages]
