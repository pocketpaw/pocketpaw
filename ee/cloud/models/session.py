"""Session document — unified chat session metadata for pocket and group contexts.

Recent change: added the optional ``surface`` field so the frontend can tell
where a session-scope row originated (``chat`` vs ``files`` vs
``pocket_creation``). Without it the three /chat / /files / /pockets
chat surfaces all produced ``Session`` rows indistinguishable on
``pocket=None`` + ``context_type="session"``, which the /chat sidebar then
listed together (the "session bleed" bug). Legacy rows keep ``surface=None``
and are still returned from unfiltered listings so the migration is non-
disruptive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from beanie import Indexed
from pydantic import Field, model_validator

from ee.cloud.models.base import TimestampedDocument

ContextType = Literal["pocket", "group", "session"]
# Surface tags the chat UI that minted this session — used by the /chat
# sidebar to filter out pocket-creation / files-panel sessions that would
# otherwise appear alongside DM threads (see "session bleed" fix).
SurfaceType = Literal["chat", "files", "pocket_creation"]


class Session(TimestampedDocument):
    """Chat session metadata, shared by pocket (1-on-1) and group contexts.

    Field names use camelCase aliases to match the frontend contract.
    """

    sessionId: Indexed(str, unique=True) = Field(alias="sessionId")  # type: ignore[valid-type]
    # context_type discriminates how the session is bound. Optional to allow
    # legacy callers that set `pocket`/`group` without an explicit type; the
    # validator below infers it when missing.
    context_type: ContextType | None = None
    pocket: str | None = None
    group: str | None = None
    agent: str | None = None
    # Surface tag — see module docstring. Optional because pre-fix rows
    # don't carry one and we deliberately don't backfill them.
    surface: SurfaceType | None = None
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    title: str = "New Chat"
    lastActivity: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="lastActivity")
    messageCount: int = Field(default=0, alias="messageCount")
    deleted_at: datetime | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _enforce_context(self) -> Session:
        # Infer context_type from field presence when not provided — keeps
        # older construction paths working during the rewrite window.
        if self.context_type is None:
            if self.group:
                self.context_type = "group"
            else:
                # Default to pocket — covers "pocket-less" sessions that still
                # hang off a sessionId with no group binding.
                self.context_type = "pocket"

        if self.context_type == "pocket":
            if self.group:
                raise ValueError("pocket session must not have group set")
        elif self.context_type == "group":
            if not self.group:
                raise ValueError("group session must have group set")
            if self.pocket:
                raise ValueError("group session must not have pocket set")
        elif self.context_type == "session":
            # Session-scope: single-user agent chat, no group/pocket anchor.
            if self.group:
                raise ValueError("session-typed session must not have group set")
            if self.pocket:
                raise ValueError("session-typed session must not have pocket set")
        return self

    class Settings:
        name = "sessions"
        indexes = [
            [("workspace", 1), ("context_type", 1), ("lastActivity", -1)],
            [("workspace", 1), ("pocket", 1), ("lastActivity", -1)],
            [("workspace", 1), ("group", 1), ("agent", 1)],
            [("workspace", 1), ("owner", 1), ("lastActivity", -1)],
            # Sidebar listing per (workspace, owner, surface) — the /chat
            # frontend's filtered query path.
            [("workspace", 1), ("owner", 1), ("surface", 1), ("lastActivity", -1)],
        ]
