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

from pocketpaw.agents.errors import AgentBackendUnavailable, AgentNotFound

if TYPE_CHECKING:
    from pocketpaw.agents.backend import AgentBackend
    from pocketpaw.soul import SoulManager

logger = logging.getLogger(__name__)


def _resolve_agent_model() -> Any:
    """Resolve the cloud ``Agent`` Beanie document class via the model registry.

    Returns the document class (a Beanie ``Document`` subclass — typed ``Any``
    here since core never imports the concrete EE type), or ``None`` on an OSS
    install with no ``pocketpaw.models`` provider registered. The agent pool is
    a cloud-only feature, so callers treat a missing model as "no such agent".
    """
    from pocketpaw._registry import first

    provider = first("pocketpaw.models")
    return provider.get_model("Agent") if provider else None


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
    # Number of in-flight ``run()`` iterations against this instance.
    # The GC must NEVER evict an instance with ``active_runs > 0`` —
    # ``last_active`` is only refreshed on yielded events, so a multi-minute
    # gap between events (e.g. while DeepSeek is in thinking mode or a slow
    # codex shell call is in progress) would otherwise look idle and the
    # GC's teardown would abort the run mid-flight. The counter is the
    # authoritative "this instance is busy" signal; ``last_active`` is just
    # for ranking idle eviction candidates.
    active_runs: int = 0


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

            agent_model = _resolve_agent_model()
            try:
                agent_doc = (
                    await agent_model.get(PydanticObjectId(agent_id)) if agent_model else None
                )
                if (
                    agent_doc
                    and agent_doc.updatedAt
                    and inst.created_from_updated_at
                    and agent_doc.updatedAt > inst.created_from_updated_at
                ):
                    # Don't rebuild while the instance has an in-flight stream
                    # — teardown would abort it. The stale config will be picked
                    # up on the next request once the current run finishes.
                    if inst.active_runs > 0:
                        logger.info(
                            "Agent %s config changed but instance is busy "
                            "(active_runs=%d); deferring rebuild",
                            agent_id,
                            inst.active_runs,
                        )
                        return inst
                    logger.info("Agent %s config changed, rebuilding", agent_id)
                    await self._teardown(inst)
                    del self._instances[agent_id]
                    return await self._build(agent_doc)
            except Exception:
                pass  # Use cached instance on DB errors
            return inst

        # Build new instance
        from beanie import PydanticObjectId

        agent_model = _resolve_agent_model()
        agent_doc = await agent_model.get(PydanticObjectId(agent_id)) if agent_model else None
        if not agent_doc:
            raise AgentNotFound(agent_id)

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
        instructions: str = "",
    ) -> AsyncIterator[Any]:
        """Run an agent on a message. Yields AgentEvent stream.

        ``instructions`` is for AUTHORITATIVE behavioral rules — surface
        conventions, delegation routing, mandatory pre-tool narration,
        etc. — and is injected directly after persona/extra without the
        "Your Knowledge Base" wrapper. Use it for anything the model
        MUST do; the wrapper around ``knowledge_context`` framed
        instructions as reference data, which models were ignoring.

        ``knowledge_context`` remains reference material (KB snippets +
        per-turn scope/participants tags). Kept under the wrapper.
        """
        instance = await self.get(agent_id)
        instance.last_active = datetime.now(UTC)

        # Build system prompt via soul bootstrap if available
        system_prompt = None
        if instance.soul_manager and instance.soul_manager.bootstrap_provider:
            try:
                ctx = await instance.soul_manager.bootstrap_provider.get_context()
                system_prompt = ctx.identity
                # Append soul-level knowledge (semantic memories, bond info, etc.)
                # into the identity block so the agent carries persistent context.
                if ctx.knowledge:
                    knowledge_lines = "\n".join(f"- {k}" for k in ctx.knowledge)
                    system_prompt = f"{system_prompt}\n\n# Key Knowledge\n{knowledge_lines}"
            except Exception:
                logger.warning("Failed to build soul prompt for agent %s", agent_id)

        # Fall back to config system_prompt or persona
        if not system_prompt:
            persona = instance.config.get("soul_persona", "")
            extra = instance.config.get("system_prompt", "")
            system_prompt = f"{persona}\n\n{extra}".strip() if persona or extra else ""

        # Authoritative behavior rules — injected BEFORE the knowledge
        # wrapper so the model reads them as instructions, not reference.
        if instructions:
            system_prompt = f"{system_prompt}\n\n{instructions}" if system_prompt else instructions

        # Query-specific soul memory recall — inject relevant past interactions
        # so the agent can reference cross-session memories. This complements
        # the general semantic facts already injected by SoulBootstrapProvider.
        if instance.soul_manager and instance.soul_manager.soul and message.strip():
            try:
                soul_ctx = await instance.soul_manager.soul.context_for(
                    message,
                    max_memories=5,
                    include_state=False,
                    include_self_model=False,
                )
                if soul_ctx:
                    memory_block = (
                        "## Relevant Past Memories\n"
                        "Below are memories from previous conversations that "
                        "are relevant to the current question. Use them to "
                        "provide continuity and a personalized response.\n\n"
                        f"{soul_ctx}"
                    )
                    if system_prompt:
                        system_prompt = f"{system_prompt}\n\n{memory_block}"
                    else:
                        system_prompt = memory_block
            except Exception:
                logger.debug("Soul context_for() failed for agent %s", agent_id)

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

        # Mark this instance as actively running for the duration of the
        # stream. ``last_active`` alone isn't enough because the LLM can have
        # multi-minute gaps between yielded events (DeepSeek thinking, slow
        # codex shell calls, etc.) — during those gaps ``last_active`` looks
        # stale and the GC would otherwise tear the instance down mid-flight,
        # which surfaces as ``AbortError`` in Codex / disconnect in others.
        # ``active_runs > 0`` is the authoritative "busy" flag the GC and
        # LRU evictor honor.
        instance.active_runs += 1
        try:
            async for event in instance.backend.run(
                message,
                system_prompt=system_prompt,
                history=history,
                session_key=session_key,
            ):
                instance.last_active = datetime.now(UTC)
                yield event
        finally:
            instance.active_runs -= 1
            instance.last_active = datetime.now(UTC)

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
            raise AgentBackendUnavailable(settings.agent_backend)
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

    async def ensure_soul(self, agent_doc: Any) -> bool:
        """Eagerly create and persist a soul for an agent, without building a backend.

        Writes ``~/.pocketpaw/souls/{workspace}/{slug}.soul`` so the soul exists
        on disk immediately after agent creation, instead of being lazily
        materialized on first chat.

        Returns True on success, False if soul is disabled or initialization failed.
        """
        from pocketpaw.config import Settings

        config = agent_doc.config
        if not getattr(config, "soul_enabled", True):
            return False

        try:
            manager = await self._init_soul(agent_doc, Settings.load())
        except Exception:
            logger.warning(
                "Failed to eagerly init soul for agent %s",
                agent_doc.id,
                exc_info=True,
            )
            return False

        try:
            await manager.shutdown()  # persists to disk
        except Exception:
            logger.warning(
                "Failed to persist eagerly-created soul for agent %s",
                agent_doc.id,
                exc_info=True,
            )
            return False
        return True

    async def _init_soul(self, agent_doc: Any, settings: Any) -> SoulManager:
        """Initialize a SoulManager for an agent."""
        from pocketpaw.config import get_config_dir
        from pocketpaw.soul import SoulManager

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
        """Evict the least recently used IDLE instance.

        Skips instances with ``active_runs > 0`` — evicting a busy instance
        would call ``backend.stop()`` and abort its in-flight stream.
        """
        idle = [(aid, inst) for aid, inst in self._instances.items() if inst.active_runs == 0]
        if not idle:
            logger.warning(
                "AgentPool at capacity but every instance is busy — "
                "skipping LRU eviction this cycle"
            )
            return
        oldest_id, _ = min(idle, key=lambda kv: kv[1].last_active)
        inst = self._instances.pop(oldest_id)
        await self._teardown(inst)
        logger.info("AgentPool: evicted LRU agent %s", inst.agent_name)

    async def _gc_loop(self) -> None:
        """Periodically evict idle instances.

        Instances with ``active_runs > 0`` are NEVER expired even if their
        ``last_active`` looks stale — the LLM may be thinking with no events
        flowing back. Tearing one down mid-stream surfaces as ``AbortError``.
        """
        while True:
            await asyncio.sleep(60)
            now = datetime.now(UTC)
            expired = [
                aid
                for aid, inst in self._instances.items()
                if inst.active_runs == 0
                and (now - inst.last_active).total_seconds() > self._max_idle
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
