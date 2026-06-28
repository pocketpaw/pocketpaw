"""Agent Pool — on-demand instantiation of cloud agents.

Each cloud Agent gets its own AgentBackend + SoulManager + memory namespace.
Instances are cached and evicted when idle (default 5 minutes).

Updated: 2026-05-21 — ``_build`` now translates an agent's ``config.tools``
  entries that name an opt-in in-process MCP server (see
  ``pocketpaw.tools.policy.OPT_IN_MCP_SERVERS``) into a per-agent
  ``ToolPolicy.mcp_servers_allow`` frozenset, and passes the resulting
  policy to the Claude SDK backend. This is how a cloud agent opts into
  the planner MCP server.
Updated: 2026-06-05 (feat/sites-svelte-engine) — ``run`` accepts a
  ``deny_mcp_tool_ids: frozenset[str]`` per-surface MCP-tool deny set (resolved
  from the request's ``SurfaceProfile`` in the EE chat loop) and forwards it to
  the backend's ``run`` when non-empty. Only the Claude SDK backend consumes it
  (subtracting the ids from its tool allowlist before launch); it is withheld
  from the call when empty so backends that don't accept the kwarg are
  unaffected. Non-empty only on /sites svelte-create.
Updated: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①) —
  ``run`` also accepts ``allow_sdk_tools: frozenset[str]``, the per-entity
  additive SDK-tool allowlist (resolved from the entity pocket's
  ``surface_profile.allowed_sdk_tools`` upstream). It mirrors the
  ``deny_mcp_tool_ids`` threading EXACTLY: a plain ``frozenset[str]`` forwarded
  to the backend's ``run`` ONLY when non-empty (so the 6 non-Claude backends
  keep their narrower signature), and consumed only by the Claude SDK backend,
  which UNIONs it into the allowlist BEFORE subtracting the deny set —
  precedence ``effective = (agent_tools ∪ allow) − deny`` (deny is the hard
  cap). Empty for every legacy / non-entity run, so ordinary runs are untouched.
  No ``pocketpaw_ee`` symbol crosses the boundary — only the frozenset.
Updated: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A1/A2) —
  ``run`` grows two more entity-profile knobs, both plain data across the EE→OSS
  boundary:
   * ``system_message_override: str | None`` — when set, REPLACES the base
     persona/soul identity portion of the system prompt while the downstream
     layers (authoritative ``instructions`` incl. ripple LAW, soul memory recall,
     knowledge-base wrapper) STILL append. So the final prompt is
     ``override + instructions + soul-memory + knowledge``. ``None`` = unchanged.
   * ``skill_names: frozenset[str]`` — the per-entity skill subset, forwarded to
     the Claude SDK backend (withhold-when-empty, same idiom as deny/allow). The
     SDK backend materializes those skills into a throwaway local plugin so ONLY
     the named skills are surfaced. Empty = legacy all-skills behavior.
Updated: 2026-06-13 (feat/claude-sdk-prewarm) — added ``prewarm``, which eagerly
  warms the agent's CLI subprocess for a session before its first turn (only the
  Claude SDK backend has one; no-op elsewhere). ``run``'s system-prompt assembly
  was factored into a shared ``_assemble_system_prompt`` so ``prewarm`` builds
  the IDENTICAL prompt the first turn will — the backend's warm-client cache key
  hashes the prompt's stable behavioral prefix, so a divergent prefix would make
  turn 1 evict the prewarmed client. ``prewarm`` folds in the agent's own skills
  (``skill_refs`` + enabled-plugin skills) exactly as ``run`` does so the plugin
  digest matches too. Fire-and-forget: never raises (the backend's ``prewarm``
  swallows its own errors; this guards instance load + prompt assembly). The EE
  trigger fires it as a background task in ``run_core.execute_run``.
Updated: 2026-06-26 (feat/mcg-3-pool-route-model) — ``_build`` now routes the
  per-agent ``config.model`` onto the correct backend Settings field via
  ``pocketpaw.llm.providers.base.route_model`` instead of the old brittle
  ``"claude" in backend`` / ``"openai" in backend`` / ``"google" in backend``
  substring chain. That chain silently dropped the per-agent model for
  ``codex_cli``, ``opencode``, ``deep_agents``, ``copilot_sdk`` and
  ``langchain_react`` (and even wrote OpenAI Agents' model to the wrong field,
  ``openai_model`` instead of ``openai_agents_model``). ``route_model`` is
  driven by ``_BACKEND_MODEL_ATTR`` (+ the langchain_react->deep_agents_model
  alias) so it covers every registered backend and preserves the composite
  ``provider:model`` / ``provider/model`` formats verbatim. Legacy backend names
  are resolved through ``_LEGACY_BACKENDS`` before routing so old agent docs
  still map to the right field. Empty model stays a no-op (backend default).
Updated: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — soft-disable enforcement.
  ``get`` now raises ``AgentDisabled`` whenever the resolved agent doc carries
  ``disabled=True``, on BOTH the cached-instance path (checked before the
  staleness branch, re-raised past the broad DB-error guard so it fails closed)
  and the cold-build path. A disabled agent is therefore unresolvable on every
  run path at once — chat SSE, group/DM bridge, planner — while in-flight runs
  keep their already-resolved instance. Added ``invalidate(agent_id)`` for the
  service to drop a cached instance the instant the flag flips (immediate
  revoke; the staleness check is only a fallback). ``invalidate`` does not tear
  down the backend so a live run is not aborted mid-stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pocketpaw.agents.errors import (
    AgentBackendUnavailable,
    AgentDisabled,
    AgentNotFound,
)

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
                # Soft-disable / revoke-everywhere (AW-4): a disabled agent is
                # unresolvable on every NEW request even if an instance is
                # cached. Checked BEFORE the staleness branch so a cached
                # instance is never handed back for a disabled agent; the
                # ``disable()`` service call also evicts the cache explicitly,
                # this is the defense-in-depth check for any path that didn't.
                if agent_doc is not None and getattr(agent_doc, "disabled", False):
                    raise AgentDisabled(agent_id)
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
            except AgentDisabled:
                # Must NOT be swallowed by the broad DB-error guard below — a
                # disabled agent has to fail closed, not fall back to the
                # cached instance.
                raise
            except Exception:
                pass  # Use cached instance on DB errors
            return inst

        # Build new instance
        from beanie import PydanticObjectId

        agent_model = _resolve_agent_model()
        agent_doc = await agent_model.get(PydanticObjectId(agent_id)) if agent_model else None
        if not agent_doc:
            raise AgentNotFound(agent_id)
        # Soft-disable / revoke-everywhere (AW-4): the same fail-closed check on
        # the cold-build path so a disabled agent can never be instantiated.
        if getattr(agent_doc, "disabled", False):
            raise AgentDisabled(agent_id)

        async with self._build_lock:
            # Double-check after acquiring lock
            if agent_id in self._instances:
                return self._instances[agent_id]
            # Evict oldest if at capacity
            if len(self._instances) >= self._max_instances:
                await self._evict_oldest()
            return await self._build(agent_doc)

    async def invalidate(self, agent_id: str) -> None:
        """Drop any cached instance for ``agent_id`` so the next ``get`` rebuilds.

        Used by the agents service on soft-disable / enable (AW-4) for IMMEDIATE
        cache invalidation — the staleness check in ``get`` is a fallback, but a
        disabled agent must be revoked the instant the flag flips, not on the
        next config-bump-detecting request. Idempotent: a no-op if uncached.
        Does NOT tear down the backend (no ``_teardown``) so an instance with an
        in-flight run is not aborted mid-stream — the entry is simply removed
        from the cache and the live ``run`` retains its own reference until it
        completes; the NEXT ``get`` rebuilds (or raises ``AgentDisabled``).
        """
        self._instances.pop(agent_id, None)

    async def _assemble_system_prompt(
        self,
        instance: AgentInstance,
        *,
        agent_id: str,
        message: str,
        instructions: str,
        knowledge_context: str,
        system_message_override: str | None,
    ) -> str | None:
        """Build the agent's system prompt from soul/persona + override +
        instructions + per-message soul recall + knowledge wrapper.

        Factored out of ``run`` (feat/claude-sdk-prewarm) so ``prewarm`` builds
        the IDENTICAL prompt the first real turn will. The Claude SDK warm-client
        cache key hashes the prompt's STABLE behavioral prefix (soul/persona +
        override + ``instructions``); the volatile tail this also appends
        (``## Relevant Past Memories`` soul recall, ``## Your Knowledge Base``)
        is stripped before hashing, so a prewarm that passes ``message=""`` /
        ``knowledge_context=""`` still hashes to the SAME prefix as the run that
        passes the real values — which is exactly what makes the prewarmed client
        reused rather than evicted on turn 1.
        """
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

        # Per-entity system-message override (entity-rooms A1): SWAP the base,
        # KEEP the layers. Everything assembled ABOVE this point is the base
        # persona/soul identity — exactly what the override replaces. The
        # downstream layers (authoritative ``instructions`` incl. the ripple LAW,
        # the soul-memory recall, the knowledge wrapper) are appended BELOW, so
        # they still ride on top of the override. ``None`` leaves the base
        # untouched (legacy path). Applied here so a backend never needs to know
        # the override exists — it rides the existing ``system_prompt`` channel.
        if system_message_override is not None:
            system_prompt = system_message_override

        # Authoritative behavior rules — injected BEFORE the knowledge
        # wrapper so the model reads them as instructions, not reference.
        if instructions:
            system_prompt = f"{system_prompt}\n\n{instructions}" if system_prompt else instructions

        # Query-specific soul memory recall — inject relevant past interactions
        # so the agent can reference cross-session memories. This complements
        # the general semantic facts already injected by SoulBootstrapProvider.
        # Skipped on an empty message (e.g. prewarm) — and stripped from the
        # cache key's behavioral prefix regardless, so it never affects reuse.
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

        return system_prompt

    async def prewarm(
        self,
        agent_id: str,
        session_key: str,
        *,
        instructions: str = "",
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        system_message_override: str | None = None,
        skill_names: frozenset[str] = frozenset(),
    ) -> None:
        """Eagerly warm the agent's CLI subprocess for ``session_key`` before its
        first turn, so the first ``run`` reuses it instead of paying the cold
        ~12s ``connect()`` (feat/claude-sdk-prewarm).

        Only the Claude SDK backend has a warm-client subprocess, so this no-ops
        on every other backend (it checks for a ``prewarm`` attribute). It
        assembles the SAME system prompt and folds in the SAME agent-own skills
        (``skill_refs`` + enabled-plugin skills) that ``run`` will, so the
        backend computes a matching cache key — a mismatched key would make the
        first turn EVICT the prewarmed client (a net loss).

        FIRE-AND-FORGET: the call is designed to be wrapped in
        ``asyncio.create_task`` by the caller. It NEVER raises — the backend's
        ``prewarm`` swallows its own errors, and this method guards instance load
        + prompt assembly in try/except. A failed prewarm leaves the next ``run``
        to pay the cold connect it would have paid anyway (no regression).

        IMPORTANT: the caller must only invoke this when the first turn's model
        is message-INDEPENDENT (smart routing OFF). With smart routing ON the
        model is classified from the message, which prewarm doesn't have, so a
        prewarm could warm the wrong model tier and cause evict-churn. The
        run_core trigger gates on this.
        """
        try:
            instance = await self.get(agent_id)
        except Exception:
            logger.debug("prewarm: could not load agent %s (skipped)", agent_id)
            return

        # Only the Claude SDK backend has a warm subprocess to prewarm.
        backend_prewarm = getattr(instance.backend, "prewarm", None)
        if backend_prewarm is None:
            return

        # Fold the agent's OWN skills into the set, mirroring ``run`` exactly so
        # the plugin digest (and thus the cache key) matches the first turn.
        cfg = getattr(instance, "config", None) or {}
        own_skills = (
            frozenset(cfg.get("skill_refs", []) or []) if isinstance(cfg, dict) else frozenset()
        )
        effective_skills = skill_names | own_skills

        try:
            system_prompt = await self._assemble_system_prompt(
                instance,
                agent_id=agent_id,
                message="",  # no turn yet — the volatile tail is stripped anyway
                instructions=instructions,
                knowledge_context="",  # stripped from the cache-key prefix
                system_message_override=system_message_override,
            )
        except Exception:
            logger.debug("prewarm: prompt assembly failed for %s (skipped)", agent_id)
            return

        # The backend's prewarm swallows ALL of its own errors, so this is
        # already safe; the outer guards above cover instance/prompt failures.
        prewarm_kwargs: dict[str, Any] = {
            "session_key": session_key,
            "system_prompt": system_prompt,
        }
        if deny_mcp_tool_ids:
            prewarm_kwargs["deny_mcp_tool_ids"] = deny_mcp_tool_ids
        if allow_sdk_tools:
            prewarm_kwargs["allow_sdk_tools"] = allow_sdk_tools
        if allow_mcp_tool_ids is not None:
            prewarm_kwargs["allow_mcp_tool_ids"] = allow_mcp_tool_ids
        if effective_skills:
            prewarm_kwargs["skill_names"] = effective_skills
        await backend_prewarm(**prewarm_kwargs)

    async def run(
        self,
        agent_id: str,
        message: str,
        session_key: str,
        history: list[dict] | None = None,
        knowledge_context: str = "",
        instructions: str = "",
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        system_message_override: str | None = None,
        skill_names: frozenset[str] = frozenset(),
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

        ``deny_mcp_tool_ids`` is a per-surface MCP-tool deny set (resolved
        from the request's ``SurfaceProfile`` upstream). It is forwarded to
        the backend's ``run`` ONLY when non-empty so it reaches the Claude
        SDK backend — the one backend that consumes it — without passing an
        unexpected kwarg to backends that don't accept it. Empty for every
        surface except /sites svelte-create, so ordinary runs are untouched.

        ``allow_sdk_tools`` is the per-entity ADDITIVE SDK-tool allowlist
        (resolved from the entity pocket's ``surface_profile.allowed_sdk_tools``
        upstream — entity-rooms chunk ①). It mirrors ``deny_mcp_tool_ids``
        exactly: forwarded to the backend's ``run`` ONLY when non-empty, and
        consumed only by the Claude SDK backend, which UNIONs it into the
        allowlist BEFORE subtracting the deny set (``(agent ∪ allow) − deny``).
        Empty for every legacy / non-entity run.

        ``system_message_override`` (entity-rooms A1), when set, REPLACES the base
        persona/soul identity portion of the assembled system prompt — the text
        built from soul bootstrap / persona / config ``system_prompt`` BEFORE the
        authoritative ``instructions`` are appended. The override SWAPS that base
        but KEEPS every downstream layer: ``instructions`` (incl. the ripple LAW),
        the query-specific soul-memory recall, and the knowledge-base wrapper all
        still append. Net prompt = ``override + instructions + soul-memory +
        knowledge``. ``None`` (the default / legacy path) leaves the base intact.

        ``skill_names`` (entity-rooms A2) is the per-entity skill subset. It is
        forwarded to the backend's ``run`` ONLY when non-empty (same
        withhold-when-empty idiom as deny/allow, so the 6 non-Claude backends keep
        their narrower signature). The Claude SDK backend materializes exactly
        those skills into a throwaway local plugin so ONLY the named skills are
        surfaced to the agent for this run. Empty = legacy all-skills behavior.
        """
        instance = await self.get(agent_id)
        instance.last_active = datetime.now(UTC)

        # An agent's OWN declared skills (config.skill_refs) must materialize on
        # EVERY run path, not only entity-room runs that thread skill_names in.
        # The SSE chat path resolves this upstream and passes skill_names; the
        # group/DM bridge (agent_bridge) calls run() without it, so a deployment
        # agent with skill_refs got none of its skills. Union the agent's own
        # skill_refs here so run() is the single source of truth for them; the
        # passed-in skill_names stays an additive per-entity subset.
        cfg = getattr(instance, "config", None) or {}
        own_skills = (
            frozenset(cfg.get("skill_refs", []) or []) if isinstance(cfg, dict) else frozenset()
        )
        if own_skills:
            skill_names = skill_names | own_skills

        # Assemble the system prompt (factored into ``_assemble_system_prompt``
        # so ``prewarm`` builds the SAME prompt this run will — the warm-client
        # cache key hashes the prompt's stable behavioral prefix, so a divergent
        # prefix would make the first turn evict the prewarmed client).
        system_prompt = await self._assemble_system_prompt(
            instance,
            agent_id=agent_id,
            message=message,
            instructions=instructions,
            knowledge_context=knowledge_context,
            system_message_override=system_message_override,
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
            # Only forward the deny set when non-empty: the Claude SDK backend
            # accepts ``deny_mcp_tool_ids``, but the other backends keep the
            # narrower ``(message, *, system_prompt, history, session_key)``
            # signature, so passing the kwarg to them would raise TypeError.
            # The set is empty for every surface except /sites svelte-create
            # (which always runs on the Claude SDK backend), so this never
            # withholds a needed deny from a backend that would honor it.
            run_kwargs: dict[str, Any] = {
                "system_prompt": system_prompt,
                "history": history,
                "session_key": session_key,
            }
            if deny_mcp_tool_ids:
                run_kwargs["deny_mcp_tool_ids"] = deny_mcp_tool_ids
            # Same withhold-when-empty rule as the deny set: only the Claude SDK
            # backend accepts ``allow_sdk_tools``; the others keep the narrower
            # signature, so an entity allowlist is forwarded only when non-empty.
            if allow_sdk_tools:
                run_kwargs["allow_sdk_tools"] = allow_sdk_tools
            # Per-MODE restrictive MCP allow-list. Forwarded only when not None
            # (the Claude SDK backend is the only consumer); None = no
            # restriction, so broad surfaces / non-Claude backends are untouched.
            if allow_mcp_tool_ids is not None:
                run_kwargs["allow_mcp_tool_ids"] = allow_mcp_tool_ids
            # Per-entity skill subset (entity-rooms A2). Same withhold-when-empty
            # rule: only the Claude SDK backend accepts ``skill_names`` (it
            # materializes them into a per-run local plugin); the other backends
            # keep the narrower signature, so the subset is forwarded only when
            # non-empty. Empty = legacy all-skills advertise behavior.
            if skill_names:
                run_kwargs["skill_names"] = skill_names
            async for event in instance.backend.run(message, **run_kwargs):
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
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.registry import _LEGACY_BACKENDS, get_backend_class
        from pocketpaw.config import Settings
        from pocketpaw.llm.providers.base import route_model
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS, ToolPolicy

        agent_id = str(agent_doc.id)
        config = agent_doc.config.model_dump()

        # Clone settings and override with agent config
        settings = Settings.load()
        settings.agent_backend = config.get("backend", "claude_agent_sdk")

        # Map the per-agent model onto the Settings field the chosen backend
        # actually reads, for EVERY registered backend — the old ``"claude" in
        # backend`` substring routing silently dropped the model for codex_cli /
        # opencode / deep_agents / copilot_sdk / langchain_react. ``route_model``
        # is the single source of truth (driven by ``_BACKEND_MODEL_ATTR`` +
        # the langchain_react->deep_agents alias) and preserves the composite
        # ``provider:model`` / ``provider/model`` formats verbatim. An empty
        # model is a no-op, so the backend falls through to its own default.
        # Catalog existence-validation is NOT done here — it lives upstream in
        # the picker / EE catalog layer (MCG-1/4); OSS can't import EE.
        # Resolve legacy backend names to their canonical key first so an old
        # agent doc (e.g. ``claude_code``) still routes to the right field.
        model = config.get("model", "")
        canonical_backend = _LEGACY_BACKENDS.get(settings.agent_backend, settings.agent_backend)
        route_model(settings, canonical_backend, model)

        # Instantiate backend
        backend_cls = get_backend_class(settings.agent_backend)
        if not backend_cls:
            raise AgentBackendUnavailable(settings.agent_backend)
        # Only the Claude SDK backend reads an injected policy. Branch on
        # the resolved class (not ``settings.agent_backend``) so legacy
        # backend names that remap to ClaudeSDKBackend are handled too;
        # every other backend's ``__init__`` accepts only ``settings``, so
        # passing ``policy=`` to one would raise TypeError.
        if backend_cls is ClaudeSDKBackend:
            # Per-agent tool policy. The agent's ``tools`` list may name
            # built-in in-process MCP servers (e.g. ``pocketpaw_planner``);
            # any token in ``OPT_IN_MCP_SERVERS`` becomes an
            # ``mcp_servers_allow`` entry. Tokens are the plain server name,
            # not the internal ``mcp:<server>:*`` notation — this is the
            # only translation point. Unknown tokens are dropped. Profile /
            # allow / deny carry the same values as the process-wide policy
            # — only ``mcp_servers_allow`` is per-agent, so opting one agent
            # into the planner never affects any other tool or external MCP
            # server (see ToolPolicy.__init__). Built only here because no
            # other backend reads an injected policy.
            mcp_servers_allow = frozenset(
                t for t in config.get("tools", []) if t in OPT_IN_MCP_SERVERS
            )
            agent_policy = ToolPolicy(
                profile=settings.tool_profile,
                allow=settings.tools_allow,
                deny=settings.tools_deny,
                mcp_servers_allow=mcp_servers_allow,
            )
            backend = backend_cls(settings, policy=agent_policy)
        else:
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
