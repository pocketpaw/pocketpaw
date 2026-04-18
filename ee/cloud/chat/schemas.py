"""Request/response and WebSocket message schemas for chat."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# REST — Requests
# ---------------------------------------------------------------------------


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    type: Literal["public", "private", "dm"] = "public"
    member_ids: list[str] = Field(default_factory=list)
    icon: str = ""
    color: str = ""


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None


class AddGroupMembersRequest(BaseModel):
    user_ids: list[str]


class AddGroupAgentRequest(BaseModel):
    agent_id: str
    role: str = "assistant"
    respond_mode: str = "auto"


class UpdateGroupAgentRequest(BaseModel):
    respond_mode: str


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    reply_to: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)


class ReactRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# REST — Responses
# ---------------------------------------------------------------------------


class MessageResponse(BaseModel):
    id: str
    group: str
    sender: str | None
    sender_type: str
    sender_name: str = ""
    content: str
    mentions: list[dict]
    reply_to: str | None
    attachments: list[dict]
    reactions: list[dict]
    edited: bool
    edited_at: datetime | None
    deleted: bool
    created_at: datetime


class GroupResponse(BaseModel):
    id: str
    workspace: str
    name: str
    slug: str
    description: str
    type: str
    icon: str
    color: str
    owner: str
    members: list[Any]  # User IDs or populated objects
    agents: list[Any]
    pinned_messages: list[str]
    archived: bool
    last_message_at: datetime | None
    message_count: int
    created_at: datetime


class CursorPage(BaseModel):
    """Cursor-based pagination response."""

    items: list[MessageResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# WebSocket Schemas
# ---------------------------------------------------------------------------


class WsInbound(BaseModel):
    """Validated inbound WebSocket message from client."""

    type: Literal[
        "message.send",
        "message.edit",
        "message.delete",
        "message.react",
        "typing.start",
        "typing.stop",
        "presence.update",
        "read.ack",
    ]
    group_id: str | None = None
    message_id: str | None = None
    content: str | None = None
    reply_to: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)
    emoji: str | None = None
    status: str | None = None


class WsOutbound(BaseModel):
    """Outbound WebSocket message to client."""

    type: str
    data: dict = Field(default_factory=dict)
