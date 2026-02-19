"""Agent Router — registry-based backend selection.

Uses the backend registry to lazily discover and instantiate the
configured agent backend. Falls back to ``claude_agent_sdk`` when
the requested backend is unavailable.
"""

import logging
from collections.abc import AsyncIterator

from pocketpaw.agents.backend import BackendInfo
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.registry import get_backend_class, get_backend_info
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class AgentRouter:
    """Routes agent requests to the selected backend via the registry."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._backend = None
        self._initialize_backend()

    def _initialize_backend(self) -> None:
        """Initialize the selected agent backend from the registry."""
        backend_name = self.settings.agent_backend

        cls = get_backend_class(backend_name)
        if cls is None:
            logger.warning(
                "Backend '%s' unavailable — falling back to claude_agent_sdk",
                backend_name,
            )
            cls = get_backend_class("claude_agent_sdk")
            backend_name = "claude_agent_sdk"

        if cls is None:
            logger.error("No agent backend could be loaded")
            return

        try:
            self._backend = cls(self.settings)
            info = cls.info()
            logger.info("🚀 Backend: %s", info.display_name)
        except Exception as exc:
            logger.error("Failed to initialize '%s' backend: %s", backend_name, exc)

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent, yielding AgentEvent objects."""
        if not self._backend:
            yield AgentEvent(type="error", content="No agent backend initialized")
            yield AgentEvent(type="done", content="")
            return

        async for event in self._backend.run(
            message, system_prompt=system_prompt, history=history, session_key=session_key
        ):
            yield event

    async def stop(self) -> None:
        """Stop the agent."""
        if self._backend:
            await self._backend.stop()

    def get_backend_info(self) -> BackendInfo | None:
        """Return metadata about the active backend."""
        if self._backend is None:
            return None
        return get_backend_info(self.settings.agent_backend)
