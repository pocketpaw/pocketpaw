"""Group document — multi-user channels with agent participants."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

# Group member role tiers (ordered by privilege, ascending):
#   "view"            — read-only
#   "edit"            — post/react (the default; absence from member_roles means "edit")
#   "post_no_mention" — can post but @mentions are blocked
#   "post_no_media"   — can post but file attachments are blocked
#   "admin"           — can modify group settings, add/remove members & agents
# The group's `owner` field is the implicit top tier (not stored here).
MemberRole = Literal["view", "edit", "post_no_media", "admin"]


class GroupAgent(BaseModel):
    """Agent assigned to a group with a respond mode."""

    agent: str  # Agent ID
    role: str = "assistant"  # assistant | listener | moderator
    respond_mode: str = "mention_only"  # mention_only | auto | silent | smart


class ChannelBinding(BaseModel):
    """Binds a group to one external conversation (T-9).

    ``channel`` is an OSS ``Channel`` value (``"telegram"``, ``"whatsapp"``,
    …) and ``chat_id`` is that channel's conversation id. Together they are
    exactly the OSS ``InboundMessage.session_key`` — ``f"{channel}:{chat_id}"``
    — which is why the mirror can resolve a room from a message with no
    workspace attribution on it.

    The pair is UNIQUE across the deployment: two groups bound to one external
    conversation would double-mirror every message and double-send every
    reply, so the bind path rejects a second claim rather than allowing it.
    """

    channel: str
    chat_id: str

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"


class Group(TimestampedDocument):
    """Chat group/channel — like Slack channels with AI agents."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    slug: str = ""
    description: str = ""
    icon: str = ""
    color: str = ""
    # Default "private": only explicit members can see/read. Workspace-wide
    # readable groups are opt-in via type="public" or type="channel".
    type: str = Field(default="private", pattern="^(public|private|dm|channel)$")
    # Channel-specific visibility: "public" (all workspace members can see) or
    # "private" (only explicit members). Ignored for non-channel groups.
    visibility: str = Field(default="public", pattern="^(public|private)$")
    members: list[str] = Field(default_factory=list)  # User IDs
    # Per-member role override: "view" = read-only; absent = "edit" (default).
    # Owner is implicit and not stored here.
    member_roles: dict[str, MemberRole] = Field(default_factory=dict)
    agents: list[GroupAgent] = Field(default_factory=list)
    pinned_messages: list[str] = Field(default_factory=list)  # Message IDs
    # Active thread parent message IDs — the "thread list" shown in the sidebar.
    # When a thread is closed/resolved it's removed from this list.
    active_threads: list[str] = Field(default_factory=list)
    owner: str  # User ID
    archived: bool = False
    last_message_at: datetime | None = None
    message_count: int = 0
    # T-9: when set, this room mirrors an external channel conversation.
    # Additive + defaulted None → zero migration; an unbound group (every
    # existing row) reads None and no mirroring happens.
    channel_binding: ChannelBinding | None = None

    class Settings:
        name = "groups"
        indexes = [
            [("workspace", 1), ("slug", 1)],
            # T-9: the mirror resolves a room from (channel, chat_id) on every
            # inbound message, and that read is deployment-wide (an external
            # message carries no workspace). Indexed so the hot path is a
            # lookup rather than a collection scan.
            [("channel_binding.channel", 1), ("channel_binding.chat_id", 1)],
        ]
