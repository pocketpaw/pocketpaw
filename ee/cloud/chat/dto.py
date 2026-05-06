"""Wire DTOs for the chat module — re-exports from ``schemas.py`` plus
domain → wire mappers that convert ``chat.domain`` value objects to the
legacy wire-format dicts.

Phase 10 keeps ``schemas.py`` as the canonical home for chat-domain
Pydantic models (because chat-unify added many references to it across
agent_router/agent_service/router) and exposes them under ``dto`` for
naming consistency with the rest of the cloud modules. New code should
import from this module:

    from ee.cloud.chat.dto import SendMessageRequest, MessageResponse

A future cleanup pass can flip the canonical home if/when the
chat-unify references are migrated.
"""

from __future__ import annotations

from typing import Any

from ee.cloud._core.time import iso_utc
from ee.cloud.chat.domain import Group, Message
from ee.cloud.chat.schemas import (  # noqa: F401
    AddGroupAgentRequest,
    AddGroupMembersRequest,
    CreateGroupRequest,
    CursorPage,
    EditMessageRequest,
    GroupResponse,
    MessageResponse,
    ReactRequest,
    SendMessageRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
    UpdateMemberRoleRequest,
    UpdateUiStateRequest,
    WsInbound,
    WsOutbound,
)


def message_to_wire_dict(m: Message, *, parent: Message | None = None) -> dict[str, Any]:
    """Convert a domain ``Message`` to the legacy wire-format dict.

    Byte-equivalent to the existing ``_message_response`` in
    ``message_service.py`` so callers migrating to the repository
    abstraction don't shift the API contract.

    The optional ``parent`` is the message being replied to; when
    supplied, ``replyPreview`` is populated so the FE can render the
    inline quote without a second fetch.
    """
    return {
        "_id": m.id,
        "group": m.group,
        "sender": m.sender,
        "senderType": m.sender_type,
        "agent": m.agent,
        "content": m.content,
        "mentions": [
            {"type": x.type, "id": x.id, "display_name": x.display_name} for x in m.mentions
        ],
        "replyTo": m.reply_to,
        "replyPreview": _reply_preview(parent) if m.reply_to else None,
        "threadCount": m.thread_count,
        "attachments": [
            {"type": a.type, "url": a.url, "name": a.name, "meta": dict(a.meta)}
            for a in m.attachments
        ],
        "reactions": [{"emoji": r.emoji, "users": list(r.users)} for r in m.reactions],
        "edited": m.edited,
        "editedAt": iso_utc(m.edited_at),
        "deleted": m.deleted,
        "createdAt": iso_utc(m.created_at),
    }


_REPLY_PREVIEW_CHARS = 140


def _reply_preview(parent: Message | None) -> dict[str, Any] | None:
    """Build a small preview payload for an inline reply quote.

    Mirrors the helper in ``message_service.py``; kept here so the new
    repository-driven path doesn't need to import from the legacy
    service module.
    """
    if parent is None or parent.deleted:
        return None
    snippet = (parent.content or "")[:_REPLY_PREVIEW_CHARS]
    return {
        "id": parent.id,
        "content": snippet,
        "sender": parent.sender,
        "senderType": parent.sender_type,
        "agent": parent.agent,
    }


def group_to_wire_dict(
    group: Group,
    *,
    users_by_id: dict[str, dict[str, str]] | None = None,
    agents_by_id: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Convert a domain ``Group`` to the legacy wire-format dict.

    ``users_by_id`` and ``agents_by_id`` provide pre-batched lookups
    (typically populated by the caller in a single Mongo query each)
    so list-views avoid the N+1 of fetching users/agents per group.

    Each user dict has shape ``{_id, name, email, avatar}``.
    Each agent dict has shape ``{_id, name, uname, avatar}`` (slug
    becomes uname; agent_id is preserved separately on the membership).
    """
    users_by_id = users_by_id or {}
    agents_by_id = agents_by_id or {}

    populated_members = []
    for uid in group.members:
        u = users_by_id.get(uid)
        if u:
            populated_members.append(
                {
                    "_id": u["_id"],
                    "name": u["name"],
                    "email": u["email"],
                    "avatar": u.get("avatar", ""),
                }
            )
        else:
            populated_members.append({"_id": uid, "name": uid, "email": ""})

    populated_agents = []
    for ga in group.agents:
        a = agents_by_id.get(ga.agent_id)
        populated_agents.append(
            {
                "_id": a["_id"] if a else ga.agent_id,
                "agent": ga.agent_id,
                "name": a["name"] if a else "Agent",
                "uname": a.get("uname", "") if a else "",
                "avatar": a.get("avatar", "") if a else "",
                "role": ga.role,
                "respond_mode": ga.respond_mode,
            }
        )

    return {
        "_id": group.id,
        "workspace": group.workspace_id,
        "name": group.name,
        "slug": group.slug,
        "description": group.description,
        "type": group.type,
        "icon": group.icon,
        "color": group.color,
        "owner": group.owner,
        "members": populated_members,
        "memberRoles": dict(group.member_roles),
        "agents": populated_agents,
        "pinnedMessages": list(group.pinned_messages),
        "archived": group.archived,
        "lastMessageAt": iso_utc(group.last_message_at),
        "messageCount": group.message_count,
        "createdAt": iso_utc(group.created_at),
    }


__all__ = [
    "AddGroupAgentRequest",
    "AddGroupMembersRequest",
    "CreateGroupRequest",
    "CursorPage",
    "EditMessageRequest",
    "GroupResponse",
    "MessageResponse",
    "ReactRequest",
    "SendMessageRequest",
    "UpdateGroupAgentRequest",
    "UpdateGroupRequest",
    "UpdateMemberRoleRequest",
    "UpdateUiStateRequest",
    "WsInbound",
    "WsOutbound",
    "group_to_wire_dict",
    "message_to_wire_dict",
]
