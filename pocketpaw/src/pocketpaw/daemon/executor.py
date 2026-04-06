"""
IntentionExecutor - Executes intentions by invoking the agent.

When an intention triggers:
1. Gather context from configured sources
2. Apply context to prompt template
3. Invoke AgentRouter with the prepared prompt
4. Stream results to callback (WebSocket/Telegram)
"""

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from ..agents.router import AgentRouter
from ..config import Settings, get_settings
from .context import ContextHub, get_context_hub
from .intentions import IntentionStore, get_intention_store

logger = logging.getLogger(__name__)


class IntentionExecutor:
    """
    Executes intentions by invoking the configured agent.

    Handles the full lifecycle of intention execution:
    - Context gathering
    - Prompt preparation
    - Agent invocation
    - Result streaming
    """

    def __init__(
        self,
        settings: Settings | None = None,
        intention_store: IntentionStore | None = None,
        context_hub: ContextHub | None = None,
    ):
        """
        Initialize the executor.

        Args:
            settings: Settings instance (uses singleton if not provided)
            intention_store: IntentionStore instance (uses singleton if not provided)
            context_hub: ContextHub instance (uses singleton if not provided)
        """
        self.settings = settings or get_settings()
        self.intention_store = intention_store or get_intention_store()
        self.context_hub = context_hub or get_context_hub()

        # Callback for streaming results
        self.stream_callback: Callable | None = None

        # Agent router (created lazily)
        self._agent_router: AgentRouter | None = None

    def _get_agent_router(self) -> AgentRouter:
        """Get or create the agent router."""
        if self._agent_router is None:
            self._agent_router = AgentRouter(self.settings)
        return self._agent_router

    def set_stream_callback(self, callback: Callable) -> None:
        """
        Set callback for streaming execution results.

        Args:
            callback: Async function to receive stream chunks.
                      Signature: async def callback(intention_id: str, chunk: dict)
        """
        self.stream_callback = callback

    async def execute(self, intention: dict) -> AsyncIterator[dict]:
        """
        Execute an intention and yield result chunks.

        Args:
            intention: Intention dict to execute

        Yields:
            Chunks from the agent execution
        """
        intention_id = intention["id"]
        intention_name = intention["name"]

        logger.info(f"Executing intention: {intention_name}")

        # Notify start
        yield {
            "type": "intention_started",
            "intention_id": intention_id,
            "intention_name": intention_name,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

        try:
            # 1. Gather context
            context_sources = intention.get("context_sources", [])
            context = {}

            if context_sources:
                logger.debug(f"Gathering context from: {context_sources}")
                context = await self.context_hub.gather(context_sources)

            # 2. Prepare prompt
            raw_prompt = intention.get("prompt", "")
            prepared_prompt = self.context_hub.apply_template(raw_prompt, context)

            logger.debug(f"Prepared prompt: {prepared_prompt[:100]}...")

            # 3. Invoke agent
            agent = self._get_agent_router()

            async for chunk in agent.run(prepared_prompt):
                yield chunk

            # 4. Mark intention as run
            self.intention_store.mark_run(intention_id)

            # Notify completion
            yield {
                "type": "intention_completed",
                "intention_id": intention_id,
                "intention_name": intention_name,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }

        except Exception as e:
            logger.error(f"Error executing intention {intention_name}: {e}")

            yield {
                "type": "intention_error",
                "intention_id": intention_id,
                "intention_name": intention_name,
                "error": str(e),
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }

    async def execute_and_stream(self, intention: dict) -> None:
        """
        Execute an intention and stream results to callback.

        Args:
            intention: Intention dict to execute
        """
        if not self.stream_callback:
            logger.warning("No stream callback set, results will be discarded")

        async for chunk in self.execute(intention):
            if self.stream_callback:
                try:
                    await self.stream_callback(intention["id"], chunk)
                except Exception as e:
                    logger.error(f"Error in stream callback: {e}")

    async def execute_by_id(self, intention_id: str) -> AsyncIterator[dict]:
        """
        Execute an intention by ID.

        Args:
            intention_id: ID of the intention to execute

        Yields:
            Chunks from the agent execution
        """
        intention = self.intention_store.get_by_id(intention_id)

        if not intention:
            yield {
                "type": "intention_error",
                "intention_id": intention_id,
                "error": f"Intention not found: {intention_id}",
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
            return

        async for chunk in self.execute(intention):
            yield chunk

    def reset_agent(self) -> None:
        """Reset the agent router (e.g., after settings change)."""
        self._agent_router = None
        logger.info("Agent router reset")
