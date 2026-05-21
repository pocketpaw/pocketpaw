"""Request and SSE-event payload schemas for the enterprise agent chat endpoint.

The endpoint lives at ``POST /cloud/chat/{scope}/{scope_id}/agent`` and streams
back a typed SSE event sequence. See
``docs/superpowers/specs/2026-04-23-enterprise-agent-chat-endpoint-design.md``
for the full protocol.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class CloudAgentChatRequest(BaseModel):
    """Body of ``POST /cloud/chat/{scope}/{scope_id}/agent``."""

    content: str = Field(min_length=1, max_length=10_000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: str | None = None
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    # Required for group scope when the group has more than one agent member;
    # optional for dm (the agent peer is unambiguous) and pocket (primary agent
    # used unless overridden).
    agent_id: str | None = None
    # Idempotency key echoed back in ``message.persisted`` so the client can
    # reconcile its optimistic bubble before any agent output arrives.
    client_message_id: str | None = None
    # Optional dispatch hint from the client. One of:
    #   - ``pocket_create`` — swaps the system prompt to pocket-creation
    #     guidance so the agent uses ``create_pocket`` instead of
    #     rendering an inline ``ui-spec`` block. This is the only value
    #     that changes backend behavior today (see ``build_context_block``).
    #   - ``skill:<name>`` — the user invoked a slash command. The Claude
    #     Agent SDK reads the bare ``/<name> args`` message text and
    #     invokes its built-in Skill tool. NOTE: ``skill:*`` is accepted
    #     but NOT yet consumed — it (and ``skill_args``) are reserved for
    #     future deterministic dispatch in ``_run_agent_stream``.
    #   - ``None`` — no hint (default; what older clients send).
    # Kept as ``str`` (not ``Literal``) so a new ``skill:<name>`` needs no
    # schema bump. The validator below still rejects values that are
    # neither ``pocket_create`` nor ``skill:``-prefixed, so a client-side
    # typo fails loudly with a 422 instead of being silently ignored.
    intent: str | None = None
    # Argument string for ``intent="skill:<name>"`` (empty when the skill
    # was invoked bare). Reserved — not consumed by the backend yet.
    skill_args: str | None = None

    @field_validator("intent")
    @classmethod
    def _check_intent(cls, v: str | None) -> str | None:
        """Reject unknown intents so client typos surface as a 422.

        ``skill:<name>`` stays open-ended (any skill name) for forward
        compatibility; only genuinely unrecognized values are rejected.
        """
        if v is None or v == "pocket_create" or v.startswith("skill:"):
            return v
        raise ValueError("intent must be 'pocket_create', 'skill:<name>', or null")


class SseEventName(StrEnum):
    """Names of SSE events emitted by the cloud agent endpoint.

    Kept as an Enum so tests and consumers have a single source of truth; the
    router itself builds raw SSE frames (``event:``/``data:``) for performance.
    """

    MESSAGE_PERSISTED = "message.persisted"
    STREAM_START = "stream_start"
    THINKING = "thinking"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    CHUNK = "chunk"
    RIPPLE = "ripple"
    POCKET_CREATED = "pocket_created"
    POCKET_MUTATION = "pocket_mutation"
    ASK_USER_QUESTION = "ask_user_question"
    STREAM_END = "stream_end"
    ERROR = "error"
