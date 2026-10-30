"""Request/response and WebSocket message schemas for chat.

Changes: 2026-08-21 (fix/uncap-chat-message-content) — ``SendMessageRequest.content``
and ``EditMessageRequest.content`` LOST their ``max_length=10_000``. The cap was
never a sized decision: it arrived with the ee/cloud rebuild bulk merge (#778,
2026-04-10) in the same pass that stamped ``name<=100`` / ``emoji<=50`` /
``q<=200`` across this module, and the runtime API has always taken 100_000 on
the identical field (``pocketpaw.api.v1.schemas.chat``). 10k characters is
roughly 2.5k tokens, which a site-authoring or "recreate this page" prompt
clears easily — and the frontend composer has no matching guard, so the only
thing the cap produced was a raw pydantic ``string_too_long`` 422 mid-compose.

``min_length=1`` STAYS on both: an empty message is still a real rejection.
There is no app-level ceiling on message content any more. The effective
ceiling is MongoDB's 16MB BSON document limit, which surfaces as a 500 at
insert rather than a 422 at the edge, and no proxy body cap was found in the
deploy config.
"""

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
    content: str = Field(min_length=1)
    reply_to: str | None = None
    mentions: list[dict] = Field(default_factory=list)
    attachments: list[dict] = Field(default_factory=list)
    thread_id: str | None = None  # When set, this message is a reply in a thread


class EditMessageRequest(BaseModel):
    content: str = Field(min_length=1)


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
