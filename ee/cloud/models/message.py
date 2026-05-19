"""Message document — unified message store for pocket agent memory and group chat."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ee.cloud.models.base import TimestampedDocument

ContextType = Literal["pocket", "group", "session"]
# Roles used for pocket agent-memory messages (LLM context rows).
PocketRole = Literal["user", "assistant", "system"]


class Mention(BaseModel):
    type: str = "user"  # user | agent | everyone
    id: str = ""
    display_name: str = ""


class Attachment(BaseModel):
    type: str = "file"  # file | image | pocket | widget
    url: str = ""
    name: str = ""
    meta: dict = Field(default_factory=dict)


class Reaction(BaseModel):
    emoji: str
    users: list[str] = Field(default_factory=list)


class Message(TimestampedDocument):
    """Single message, tagged by ``context_type``.

    ``context_type="group"`` — multi-user group chat row with mentions,
    reactions, threading, and soft-delete. ``group`` is required.

    ``context_type="pocket"`` — pocket/agent memory row (LLM history).
    ``session_key`` and ``role`` are required; group-chat extras must stay
    empty.

    ``context_type="session"`` — session-scope agent conversation row
    (single-user cloud session). Same shape as "pocket": ``session_key``
    and ``role`` are required, group-chat extras must stay empty.

    All three shapes live in one Mongo collection to give callers a single
    abstraction for messages regardless of where they were sent.
    """

    # Discriminator is optional at the schema level to keep legacy call sites
    # working during the rewrite window; the validator infers it when missing.
    context_type: ContextType | None = None

    # --- Group chat fields (context_type == "group") ---------------------
    group: str | None = None
    sender: str | None = None  # User ID; None when system or agent
    sender_type: str = "user"  # user | agent
    agent: str | None = None  # Agent ID when sender_type == "agent"
    content: str = ""
    mentions: list[Mention] = Field(default_factory=list)
    reply_to: str | None = None
    thread_count: int = 0
    # Threading: when set, this message is a reply within a thread whose
    # parent is `thread_id`. When `is_thread_parent` is True, this message
    # is the first message of a thread (the parent).
    thread_id: str | None = None
    is_thread_parent: bool = False
    attachments: list[Attachment] = Field(default_factory=list)
    reactions: list[Reaction] = Field(default_factory=list)
    edited: bool = False
    edited_at: datetime | None = None
    deleted: bool = False

    # --- Pocket agent-memory fields (context_type == "pocket") ----------
    session_key: str | None = None
    role: PocketRole | None = None

    # --- Tenant scope --------------------------------------------------
    # Stamped on every row so multi-tenant ee deployments can scope reads
    # at the adapter layer. For pocket rows the adapter resolves it from
    # the linked Session.workspace at write time; for group rows callers
    # populate it from the group's workspace.
    workspace_id: str | None = None

    @model_validator(mode="after")
    def _enforce_context(self) -> Message:
        # Infer context when unset so legacy constructors (group=..., sender=...)
        # still produce a valid group-typed row.
        if self.context_type is None:
            if self.session_key or self.role:
                self.context_type = "pocket"
            else:
                self.context_type = "group"

        if self.context_type == "group":
            if not self.group:
                raise ValueError("group message must have group set")
            if self.session_key is not None:
                raise ValueError("group message must not set session_key")
            if self.role is not None:
                raise ValueError("group message must not set role")
        elif self.context_type in ("pocket", "session"):
            if not self.session_key:
                raise ValueError("pocket message must have session_key set")
            if self.role not in ("user", "assistant", "system"):
                raise ValueError(
                    f"pocket message role must be user/assistant/system, got {self.role!r}"
                )
            if self.group:
                raise ValueError("pocket message must not have group set")
            if self.mentions:
                raise ValueError("pocket message must not have mentions")
            if self.reactions:
                raise ValueError("pocket message must not have reactions")
            if self.reply_to:
                raise ValueError("pocket message must not have reply_to")
        return self

    class Settings:
        name = "messages"
        indexes = [
            [("context_type", 1), ("group", 1), ("createdAt", -1)],
            [("workspace_id", 1), ("session_key", 1), ("createdAt", 1)],
            [("session_key", 1), ("createdAt", 1)],
            [("group", 1), ("createdAt", -1)],
        ]
