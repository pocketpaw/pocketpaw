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
    type: Literal["public", "private", "dm", "channel"] = "private"
    visibility: Literal["public", "private"] = "public"
    member_ids: list[str] = Field(default_factory=list)
    icon: str = ""
    color: str = ""


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    # Toggle visibility — "private" (members-only) vs "public"/"channel"
    # (any workspace member can read). DMs cannot be retyped.
    type: Literal["public", "private", "channel"] | None = None
    visibility: Literal["public", "private"] | None = None


class AddGroupMembersRequest(BaseModel):
    user_ids: list[str]
    role: Literal["edit", "post_no_media", "view"] = "edit"


class UpdateMemberRoleRequest(BaseModel):
    role: Literal["edit", "post_no_media", "view"]


class AddGroupAgentRequest(BaseModel):
    agent_id: str
    role: str = "assistant"
    respond_mode: str = "auto"


class UpdateGroupAgentRequest(BaseModel):
    respond_mode: str


class CreateThreadRequest(BaseModel):
    """Create a thread from an existing message."""

    message_id: str = Field(..., description="The message to use as thread parent")


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    reply_to: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)
    thread_id: str | None = None  # When set, this message is a reply in a thread


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)


class ReactRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=50)


class UpdateUiStateRequest(BaseModel):
    """Patch the inline-Ripple state for one ui-spec block in a message.

    ``spec_id`` is the spec's position-based key (``spec_0``, ``spec_1``, ...).
    ``state`` is the full Ripple state map for that spec — last-write-wins
    on the entire spec_id (no field-level merge), since Ripple's
    ``onStateChange`` always carries the complete state snapshot.
    """

    spec_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    state: dict = Field(default_factory=dict)


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
    thread_id: str | None = None
    is_thread_parent: bool = False
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
        "room.join",
        "room.leave",
        "thread.create",
        "thread.close",
        "thread.send",
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
