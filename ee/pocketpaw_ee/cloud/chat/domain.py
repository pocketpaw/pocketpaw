"""Domain value objects for the chat module.

Pure-Python frozen dataclasses, no Beanie/FastAPI imports. Mirror the
persistence sub-models in ``ee.cloud.models.message`` and
``ee.cloud.models.group`` field-for-field so the repository can convert
trivially without losing structure.

Phase 10 ships only the value objects. The service+router migration to
use these is incremental — existing call sites keep using the Beanie
docs directly until each method is migrated. New code should prefer
domain types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Mirror persistence-layer literals
ContextType = Literal["pocket", "group", "session"]
PocketRole = Literal["user", "assistant", "system"]
MemberRole = Literal["view", "edit", "post_no_media", "admin"]
GroupType = Literal["public", "private", "dm", "channel"]


@dataclass(frozen=True)
class Mention:
    """Reference to a user/agent/everyone mentioned in message content."""

    type: str  # user | agent | everyone
    id: str
    display_name: str


@dataclass(frozen=True)
class Attachment:
    """File/media attached to a message."""

    type: str  # file | image | pocket | widget
    url: str
    name: str
    meta: tuple[tuple[str, object], ...] = ()  # frozen-friendly dict


@dataclass(frozen=True)
class Reaction:
    """Emoji reaction with the list of users who applied it."""

    emoji: str
    users: tuple[str, ...]


@dataclass(frozen=True)
class GroupAgent:
    """Agent participant in a group with response behavior."""

    agent_id: str
    role: str  # assistant | listener | moderator
    respond_mode: str  # mention_only | auto | silent | smart


@dataclass(frozen=True)
class Group:
    """Chat group/channel — multi-user with agent participants."""

    id: str
    workspace_id: str
    name: str
    slug: str
    description: str
    icon: str
    color: str
    type: str  # GroupType (kept as str for forward-compat)
    members: tuple[str, ...]  # user_ids
    member_roles: tuple[tuple[str, str], ...]  # (user_id, role) pairs
    agents: tuple[GroupAgent, ...]
    pinned_messages: tuple[str, ...]
    owner: str  # user_id
    archived: bool
    last_message_at: datetime | None
    message_count: int
    created_at: datetime
    updated_at: datetime
    visibility: str = "public"  # "public" | "private" — for channels
    active_threads: tuple[str, ...] = ()


@dataclass(frozen=True)
class Message:
    """A message — group, pocket, or session shape.

    Group rows have ``group`` set; pocket/session rows have ``session_key``
    and ``role`` set. The ``context_type`` discriminates and the validator
    in the persistence layer enforces the invariants.
    """

    id: str
    context_type: str  # ContextType
    workspace_id: str | None
    # Group fields
    group: str | None
    sender: str | None  # user_id
    sender_type: str  # user | agent
    agent: str | None  # agent_id when sender_type == agent
    content: str
    mentions: tuple[Mention, ...] = field(default_factory=tuple)
    reply_to: str | None = None
    thread_count: int = 0
    thread_id: str | None = None
    is_thread_parent: bool = False
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
    reactions: tuple[Reaction, ...] = field(default_factory=tuple)
    edited: bool = False
    edited_at: datetime | None = None
    deleted: bool = False
    # Pocket/session fields
    session_key: str | None = None
    role: str | None = None  # PocketRole
    # Timestamps
    created_at: datetime | None = None


__all__ = [
    "Attachment",
    "ContextType",
    "Group",
    "GroupAgent",
    "GroupType",
    "MemberRole",
    "Mention",
    "Message",
    "PocketRole",
    "Reaction",
]
