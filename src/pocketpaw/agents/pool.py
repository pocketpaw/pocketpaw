"""Agent Pool — on-demand instantiation of cloud agents.

Each cloud Agent gets its own AgentBackend + SoulManager + memory namespace.
Instances are cached and evicted when idle (default 5 minutes).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw.agents.backend import AgentBackend
    from pocketpaw.soul.manager import SoulManager

logger = logging.getLogger(__name__)


@dataclass
class AgentInstance:
    """A running agent with its own backend, soul, and memory namespace."""

    agent_id: str
    agent_name: str
    config: dict
    backend: AgentBackend
    soul_manager: SoulManager | None
    memory_namespace: str
    last_active: datetime = field(default_factory=lambda: datetime.now(UTC))
    created_from_updated_at: datetime | None = None


class AgentPool:
    """Manages running agent instances with on-demand creation and idle eviction."""

    def __init__(self, max_idle: int = 300, max_instances: int = 20) -> None:
        self._instances: dict[str, AgentInstance] = {}
        self._max_idle = max_idle
        self._max_instances = max_instances
        self._gc_task: asyncio.Task | None = None
        self._build_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the GC background task."""
        self._gc_task = asyncio.create_task(self._gc_loop())
        logger.info(
            "AgentPool started (max_idle=%ds, max_instances=%d)",
            self._max_idle,
            self._max_instances,
        )

    async def stop(self) -> None:
        """Stop all instances and the GC task."""
        if self._gc_task:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass
            self._gc_task = None
        for instance in list(self._instances.values()):
            await self._teardown(instance)
        self._instances.clear()

    async def get(self, agent_id: str) -> AgentInstance:
        """Get or create an agent instance. Fetches config from MongoDB."""
        if agent_id in self._instances:
            inst = self._instances[agent_id]
            inst.last_active = datetime.now(UTC)
            # Check config staleness
            from beanie import PydanticObjectId

            from ee.cloud.models.agent import Agent

            try:
                agent_doc = await Agent.get(PydanticObjectId(agent_id))
                if (
                    agent_doc
                    and agent_doc.updatedAt
                    and inst.created_from_updated_at
                    and agent_doc.updatedAt > inst.created_from_updated_at
                ):
                    logger.info("Agent %s config changed, rebuilding", agent_id)
                    await self._teardown(inst)
                    del self._instances[agent_id]
                    return await self._build(agent_doc)
            except Exception:
                pass  # Use cached instance on DB errors
            return inst

        # Build new instance
        from beanie import PydanticObjectId

        from ee.cloud.models.agent import Agent

        agent_doc = await Agent.get(PydanticObjectId(agent_id))
        if not agent_doc:
            from ee.cloud.shared.errors import NotFound

            raise NotFound("agent", agent_id)

        async with self._build_lock:
            # Double-check after acquiring lock
            if agent_id in self._instances:
                return self._instances[agent_id]
            # Evict oldest if at capacity
            if len(self._instances) >= self._max_instances:
                await self._evict_oldest()
            return await self._build(agent_doc)

    async def run(
        self,
        agent_id: str,
        message: str,
        session_key: str,
        history: list[dict] | None = None,
        knowledge_context: str = "",
    ) -> AsyncIterator[Any]:
        """Run an agent on a message. Yields AgentEvent stream."""
        instance = await self.get(agent_id)
        instance.last_active = datetime.now(UTC)

        # Build system prompt via soul bootstrap if available
        system_prompt = None
        if instance.soul_manager and instance.soul_manager.bootstrap_provider:
            try:
                ctx = await instance.soul_manager.bootstrap_provider.get_context()
                system_prompt = ctx.identity
            except Exception:
                logger.warning("Failed to build soul prompt for agent %s", agent_id)

        # Fall back to config system_prompt or persona
        if not system_prompt:
            persona = instance.config.get("soul_persona", "")
            extra = instance.config.get("system_prompt", "")
            system_prompt = f"{persona}\n\n{extra}".strip() if persona or extra else ""

        # Inject knowledge context directly into system prompt
        if knowledge_context:
            system_prompt = (
                f"{system_prompt}\n\n"
                "## Your Knowledge Base\n"
                "Use the following information from your knowledge base to answer questions. "
                "Always reference this data when relevant instead of "
                "making things up or using tools to search.\n\n"
                f"{knowledge_context}"
            )

        async for event in instance.backend.run(
            message,
            system_prompt=system_prompt,
            history=history,
            session_key=session_key,
        ):
            yield event

    async def observe(self, agent_id: str, user_input: str, agent_output: str) -> None:
        """Observe an interaction for soul learning."""
        inst = self._instances.get(agent_id)
        if inst and inst.soul_manager and inst.soul_manager.soul:
            try:
                await inst.soul_manager.observe(user_input, agent_output)
            except Exception:
                logger.debug("Soul observe failed for agent %s", agent_id)

    async def _build(self, agent_doc: Any) -> AgentInstance:
        """Build a new AgentInstance from an Agent document."""
        from pocketpaw.agents.registry import get_backend_class
        from pocketpaw.config import Settings

        agent_id = str(agent_doc.id)
        config = agent_doc.config.model_dump()

        # Clone settings and override with agent config
        settings = Settings.load()
        settings.agent_backend = config.get("backend", "claude_agent_sdk")

        # Map model to the correct settings field based on backend
        model = config.get("model", "")
        if model:
            if "claude" in settings.agent_backend:
                settings.claude_sdk_model = model
            elif "openai" in settings.agent_backend:
                settings.openai_model = model
            elif "google" in settings.agent_backend:
                settings.google_adk_model = model

        # Instantiate backend
        backend_cls = get_backend_class(settings.agent_backend)
        if not backend_cls:
            from ee.cloud.shared.errors import ValidationError

            raise ValidationError(
                "agent.invalid_backend",
                f"Backend '{settings.agent_backend}' not available",
            )
        backend = backend_cls(settings)

        # Initialize soul if enabled
        soul_manager = None
        if config.get("soul_enabled", True):
            try:
                soul_manager = await self._init_soul(agent_doc, settings)
            except Exception:
                logger.warning(
                    "Failed to init soul for agent %s, continuing without",
                    agent_id,
                    exc_info=True,
                )

        instance = AgentInstance(
            agent_id=agent_id,
            agent_name=agent_doc.name,
            config=config,
            backend=backend,
            soul_manager=soul_manager,
            memory_namespace=f"agent:{agent_id}",
            created_from_updated_at=agent_doc.updatedAt,
        )
        self._instances[agent_id] = instance
        logger.info("AgentPool: built instance for %s (%s)", agent_doc.name, settings.agent_backend)
        return instance

    async def _init_soul(self, agent_doc: Any, settings: Any) -> SoulManager:
        """Initialize a SoulManager for an agent."""
        from pocketpaw.config import get_config_dir
        from pocketpaw.soul.manager import SoulManager

        config = agent_doc.config

        # Override soul settings for this agent
        settings.soul_enabled = True
        settings.soul_name = agent_doc.name
        settings.soul_archetype = config.soul_archetype or f"The {agent_doc.name}"
        settings.soul_persona = config.soul_persona
        settings.soul_values = config.soul_values
        settings.soul_ocean = config.soul_ocean

        # Soul file: ~/.pocketpaw/souls/{workspace}/{slug}.soul
        soul_dir = get_config_dir() / "souls" / agent_doc.workspace
        soul_dir.mkdir(parents=True, exist_ok=True)
        settings.soul_path = str(soul_dir / f"{agent_doc.slug}.soul")

        manager = SoulManager(settings)
        await manager.initialize()
        return manager

    async def _teardown(self, instance: AgentInstance) -> None:
        """Gracefully shutdown an agent instance."""
        try:
            await instance.backend.stop()
        except Exception:
            pass
        if instance.soul_manager:
            try:
                await instance.soul_manager.shutdown()
            except Exception:
                pass
        logger.info("AgentPool: teardown %s", instance.agent_name)

    async def _evict_oldest(self) -> None:
        """Evict the least recently used instance."""
        if not self._instances:
            return
        oldest_id = min(self._instances, key=lambda k: self._instances[k].last_active)
        inst = self._instances.pop(oldest_id)
        await self._teardown(inst)
        logger.info("AgentPool: evicted LRU agent %s", inst.agent_name)

    async def _gc_loop(self) -> None:
        """Periodically evict idle instances."""
        while True:
            await asyncio.sleep(60)
            now = datetime.now(UTC)
            expired = [
                aid
                for aid, inst in self._instances.items()
                if (now - inst.last_active).total_seconds() > self._max_idle
            ]
            for aid in expired:
                inst = self._instances.pop(aid, None)
                if inst:
                    await self._teardown(inst)


# Module-level singleton
_pool: AgentPool | None = None


def get_agent_pool() -> AgentPool:
    """Get or create the global agent pool."""
    global _pool
    if _pool is None:
        _pool = AgentPool()
    return _pool
