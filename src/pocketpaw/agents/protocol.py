"""Agent Protocol — core event type and legacy agent interface.

Updated: 2026-08-15 (HTN-4, feat/claude-sdk-tool-args) — documents the
``tool_use`` metadata contract, in particular ``input_pending``. Backends differ
in how many ``tool_use`` events they emit per call (claude_sdk emits a
provisional announcement then a resolved event; deep_agents dedupes internally
and emits once), and the rule reconciling them lived only in those backends'
file headers, where no consumer would find it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class AgentEvent:
    """Standardized event from any agent backend.

    Types:
        - "message": Text content from the agent
        - "tool_use": Tool is being invoked
        - "tool_result": Tool execution result
        - "thinking": Extended thinking content (Activity panel only)
        - "thinking_done": Thinking phase completed
        - "token_usage": Token usage metadata
        - "session_id": Native SDK session id captured on turn 1 (SS-1) —
          metadata carries ``session_id`` so the controller can persist it for a
          later native ``resume``. Only emitted when a ``SessionHandle`` is threaded.
        - "error": Error message
        - "done": Agent finished processing

    ``tool_use`` metadata:
        - ``name`` (str): the tool being invoked.
        - ``input`` (dict): the arguments it was called with.
        - ``input_pending`` (bool, optional): whether ``input`` is still
          provisional. See the contract below.

    The ``input_pending`` contract — ONE tool call may produce MORE THAN ONE
    ``tool_use`` event:

        - ``input_pending=True`` marks a PROVISIONAL announcement. The backend
          knows the tool's name but its arguments have not finished arriving, so
          ``input`` is a placeholder (typically ``{}``) and is NOT the arguments
          the tool will run with. A resolved event for the same call follows.
        - ``input_pending=False`` — or the key being ABSENT — marks a RESOLVED
          event: ``input`` is final. Absent means resolved, so a backend that
          emits one event per call needs no flag and is correct by default.

    Backends may legitimately do either. ``claude_sdk`` announces a streamed call
    as it opens (the SDK reveals the name before the argument JSON has streamed)
    and then emits the resolved event, because that announcement is what makes a
    "tool started" indicator prompt. ``deep_agents`` instead dedupes on
    ``tool_call_id`` and emits once, already resolved.

    Consumer rule: if you APPEND per event — a log, an activity feed, a pending
    list keyed per call — you MUST skip events with ``input_pending is True``, or
    you will record a phantom call carrying no arguments. If you REPLACE per tool
    (a status line that gets overwritten), you may render both and let the
    resolved event upgrade the display. Test ``is True`` rather than truthiness
    so an unflagged backend keeps behaving as resolved.
    """

    type: str
    content: Any
    metadata: dict = field(default_factory=dict)


class AgentProtocol(Protocol):
    """Legacy interface kept for type-checking compatibility."""

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> AsyncIterator[AgentEvent]: ...

    async def stop(self) -> None: ...

    async def get_status(self) -> dict: ...
