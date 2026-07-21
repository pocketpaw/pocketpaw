# dto.py — Request/response DTOs for the Code Mode agent turn (CA-1).
#
# Created 2026-07-21 (feat/codeagent-turn). Distinct <Op>Request / <Op>Response
# classes per ee/cloud Rule 4, camelCase on the wire to match the rest of the
# cloud surface.
#
# The shape encodes the decision that separates this module from the
# ``websandbox.edit`` endpoint it will replace: the client sends the CONTEXT, the
# server does not go and fetch it. There is no ``rowId`` and no sandbox id here,
# because a WebContainer project has neither — it runs in the user's tab and has
# no server-side row at all. Anything the model is allowed to see arrives in
# ``context``, which is why this endpoint works identically for both runtimes.
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.codeagent.domain import (
    MAX_CONTEXT_ITEMS,
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    MAX_PATH_CHARS,
)


class ContextItem(BaseModel):
    """One piece of code the caller chose to put in front of the model.

    ``content`` is the text the CLIENT read out of its own ``CodeFileSession`` —
    the server never opens a file to build this. A selection is expressed as a
    1-based inclusive line range, matching what ``CodeEditor`` already reports to
    its Cmd-K hook, so the same shape serves both Ask and Edit.
    """

    path: str = Field(..., min_length=1, max_length=MAX_PATH_CHARS)
    content: str = Field(default="")
    startLine: int | None = Field(default=None, ge=1)
    endLine: int | None = Field(default=None, ge=1)


class AgentMessage(BaseModel):
    """One conversation turn. Only ``user`` and ``assistant`` cross the wire —
    the system prompt is server-owned and never client-settable, so a caller
    cannot rewrite the agent's instructions."""

    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=MAX_MESSAGE_CHARS)


class AgentTurnRequest(BaseModel):
    """A single stateless turn. The client owns the conversation and replays it
    each time; the server keeps nothing between calls."""

    messages: list[AgentMessage] = Field(..., min_length=1, max_length=MAX_MESSAGES)
    context: list[ContextItem] = Field(default_factory=list, max_length=MAX_CONTEXT_ITEMS)


class AgentTurnResponse(BaseModel):
    """The model's answer plus an honest account of what it was given.

    ``citedPaths`` is what actually reached the model, and ``droppedPaths`` is
    what the budget cut. Reporting the drop is the point: a silently truncated
    context produces a confidently wrong answer with no way for the user to see
    why, which is exactly the failure the visible budget indicator exists to
    prevent.
    """

    answer: str
    citedPaths: list[str] = Field(default_factory=list)
    droppedPaths: list[str] = Field(default_factory=list)
    truncated: bool = False


__all__ = [
    "AgentMessage",
    "AgentTurnRequest",
    "AgentTurnResponse",
    "ContextItem",
]
