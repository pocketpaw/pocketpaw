"""Request and SSE-event payload schemas for the enterprise agent chat endpoint.

The endpoint lives at ``POST /cloud/chat/{scope}/{scope_id}/agent`` and streams
back a typed SSE event sequence. See
``docs/superpowers/specs/2026-04-23-enterprise-agent-chat-endpoint-design.md``
for the full protocol.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


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
    # Optional intent hint that swaps the system-prompt block built by
    # ``build_context_block``. The desktop client sets ``pocket_create``
    # when the user is in the pocket sidebar with no pocket selected
    # ("describe a pocket to create…"), so the agent uses the
    # ``create_pocket`` tool instead of rendering an inline ``ui-spec``
    # block as a chat reply.
    intent: Literal["pocket_create"] | None = None


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
