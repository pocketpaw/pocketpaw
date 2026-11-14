"""Message document — unified message store for pocket agent memory and group chat.

Agent replies carry extras beyond their text:

- ``run_status``: the terminal status (``failed`` | ``cancelled`` |
  ``interrupted``) when the row is the partial text of a run that did not
  complete, so the UI can mark it cut off and the history reader can tell the
  model the reply is incomplete. ``None`` on every complete reply.
- ``steps`` / ``steps_omitted`` (display only): the ordered thinking blocks and
  tool calls the reply streamed (``chat/runs/steps.StepRecorder``).
- ``ask_user`` (display only): the question the reply asked through the ``ask_user`` tool
  (``MessageAskUser``), so the option chips survive a reload. The user's next
  message in the scope marks it answered.

All are additive with empty defaults, so legacy rows load without a migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from pocketpaw_ee.cloud.models.base import TimestampedDocument

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


class MessageStep(BaseModel):
    """One thinking block or tool call of an agent reply, in stream order.

    Written already bounded and scrubbed by ``StepRecorder``: ``input`` is the
    scrubbed argument dict, or a truncated JSON string when it was too large;
    ``output`` is redacted and capped (``output_truncated`` says so).
    ``missing_result`` marks a call whose run ended before its result arrived.
    """

    id: str
    kind: Literal["thinking", "tool"]
    tool: str = ""
    narration: str = ""
    text: str = ""  # thinking only
    input: Any = None
    output: str = ""
    output_truncated: bool = False
    status: Literal["running", "complete", "error", "missing_result"] = "running"
    started_at: datetime | None = None
    ended_at: datetime | None = None


class MessageAskUser(BaseModel):
    """The ``ask_user`` question an agent reply is waiting on.

    ``id`` is the run that asked. ``options`` are the normalized labels the
    client renders as chips (``ask_user_payload``). ``multi_select`` is ``False``
    today: the tool asks single-choice questions only. ``answered`` flips, and
    ``answer`` takes the reply's text, when the user's next message in the same
    scope lands.
    """

    id: str
    question: str
    options: list[str] = Field(default_factory=list)
    multi_select: bool = False
    answered: bool = False
    answer: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """The ``askUser`` value the history wire dicts carry (camelCase)."""
        return {
            "id": self.id,
            "question": self.question,
            "options": list(self.options),
            "multiSelect": self.multi_select,
            "answered": self.answered,
            "answer": self.answer,
        }


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
    sender_name: str | None = None  # Friendly display name (e.g. "Meeting Notes")
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

    # --- Cut-off agent replies ------------------------------------------
    # The terminal run status (``failed`` | ``cancelled`` | ``interrupted``) when
    # this assistant row is the partial text of a run that did not complete.
    # ``None`` for every complete reply and every human message.
    run_status: str | None = None

    # --- Agent work behind a reply ----------------------------------------
    # Thinking blocks and tool calls, in order, capped per message; the calls a
    # cap dropped are counted in ``steps_omitted`` rather than stored.
    steps: list[MessageStep] = Field(default_factory=list)
    steps_omitted: int = 0

    # --- Interactive question ---------------------------------------------
    # Set when the reply called ``ask_user``; ``None`` everywhere else.
    ask_user: MessageAskUser | None = None

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
            # backend-perf M3. `get_by_type(SESSION)` filters on context_type
            # ALONE and sorts -createdAt. The index above cannot serve it: with
            # `group` unbounded in the middle, createdAt can supply neither a
            # range bound nor the sort, so Mongo fetches every pocket message
            # in the collection and sorts them in memory — which does not
            # merely get slow, it HARD-FAILS past the 32 MB in-memory sort
            # limit. This two-key index makes the sort an index walk.
            [("context_type", 1), ("createdAt", -1)],
            [("workspace_id", 1), ("session_key", 1), ("createdAt", 1)],
            [("session_key", 1), ("createdAt", 1)],
            [("group", 1), ("createdAt", -1)],
            # Thread / reply reads: filter on the parent message id (globally
            # unique, so no group prefix) and walk replies oldest-first.
            [("thread_id", 1), ("createdAt", 1)],
            [("reply_to", 1), ("createdAt", 1)],
            # Unread count: ``group`` + ``_id > last_read_message_id``.
            [("group", 1), ("_id", 1)],
        ]
