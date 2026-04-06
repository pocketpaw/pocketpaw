"""Agent Protocol — core event type and legacy agent interface."""

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
        - "error": Error message
        - "done": Agent finished processing
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
