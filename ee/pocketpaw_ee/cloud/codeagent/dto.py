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
#
# Modified: 2026-07-21 (CA-4, Edit mode). ``AgentTurnRequest.mode`` selects the
# permission set. It defaults to ``ask``, so a caller that omits it gets the
# read-only tools — the failure mode of a forgotten field is "cannot edit", not
# "can edit unexpectedly".
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.codeagent.domain import (
    MAX_CONTEXT_ITEMS,
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    MAX_PATH_CHARS,
    MAX_TOOL_ITERATIONS,
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


class ToolCall(BaseModel):
    """A request from the model for the CLIENT to go and read something.

    ``id`` is the model's own correlation id and must be echoed back on the
    matching result — the conversation is invalid to the model without the pair.
    """

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=64)
    input: dict = Field(default_factory=dict)


class ToolResult(BaseModel):
    """What the client's ``CodeFileSession`` returned for one ``ToolCall``.

    ``name`` and ``input`` are echoed back because the turn is STATELESS: the
    server kept no record of what it asked for, so the client has to hand back
    enough to reconstruct both halves of the exchange for the model.

    ``isError`` distinguishes "I looked and there is nothing there" from "the
    lookup itself failed", which the model should treat differently — the first
    is an answer, the second is worth mentioning rather than silently working
    around.
    """

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=64)
    input: dict = Field(default_factory=dict)
    output: str = Field(default="")
    isError: bool = False


class AgentTurnRequest(BaseModel):
    """A single stateless step. The client owns the conversation and replays it
    each time; the server keeps nothing between calls.

    ``toolResults`` accumulates within ONE question — the model asks for files,
    the client fetches them, and both sides ride along on the next step until the
    model answers. Earlier questions are represented by their final answers only:
    replaying their tool traffic would re-send every file ever read for the rest
    of the conversation, to say something the answer already says.
    """

    messages: list[AgentMessage] = Field(..., min_length=1, max_length=MAX_MESSAGES)
    # Which permission set the turn runs under. ``ask`` is read-only; ``edit``
    # adds `writeFile`, which the CLIENT stages for per-hunk review rather than
    # applying. The default is the safe one on purpose — an omitted field, an
    # older client, or a replayed request all land in read-only.
    mode: Literal["ask", "edit"] = "ask"
    context: list[ContextItem] = Field(default_factory=list, max_length=MAX_CONTEXT_ITEMS)
    toolResults: list[ToolResult] = Field(
        default_factory=list,
        # One over the loop cap, since a client may legitimately post the results
        # of the final permitted round.
        max_length=MAX_TOOL_ITERATIONS * 8,
    )


class AgentTurnResponse(BaseModel):
    """One step's outcome: either an answer, or a request to go and look.

    ``done`` is the branch the client loops on. When it is false ``toolCalls`` is
    non-empty and the client executes them against its own ``CodeFileSession``
    and posts back; when true ``toolCalls`` is empty.

    ``answer`` may be set on EITHER branch. On a finished turn it is the answer.
    On an unfinished one it is whatever the model wrote alongside its calls —
    ignored by an Ask loop that is about to go round again, and shown by Edit,
    where a ``writeFile`` call stops the loop at the review gate and this is the
    model's account of what it proposed.

    ``citedPaths`` is what actually reached the model and ``droppedPaths`` is
    what the budget cut. Reporting the drop is the point: a silently truncated
    context produces a confidently wrong answer with no way for the user to see
    why.
    """

    done: bool = True
    answer: str = ""
    toolCalls: list[ToolCall] = Field(default_factory=list)
    citedPaths: list[str] = Field(default_factory=list)
    droppedPaths: list[str] = Field(default_factory=list)
    truncated: bool = False


__all__ = [
    "AgentMessage",
    "AgentTurnRequest",
    "AgentTurnResponse",
    "ContextItem",
    "ToolCall",
    "ToolResult",
]
