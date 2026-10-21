"""Agent Pool: on-demand instantiation of cloud agents.

Each cloud Agent gets its own AgentBackend + SoulManager + memory namespace,
built from its Agent document (``_build``: the per-agent ``config.model`` is
routed onto the field the backend reads via ``route_model``, legacy backend
names go through ``_LEGACY_BACKENDS`` first, opt-in MCP servers in
``config.tools`` become the agent's ``ToolPolicy``). Instances are cached per
process (``max_instances`` from settings), evicted when idle, rebuilt when the
document changes, and refused with ``AgentDisabled`` once the agent is disabled.
Every backend the pool drops is torn down; ``invalidate`` defers that for a busy
instance until its last run ends.

``run`` assembles the system prompt as a list of layers
(``_SYSTEM_PROMPT_LAYERS``, rendered by ``pocketpaw.prompt.assemble``) and
forwards a ``stable_digest`` of the keyed layers to a backend that declares it.
Per-turn layers ride ``turn_context`` for a backend whose ``run`` declares it;
``prewarm`` builds the identical stable prompt so turn 1 reuses the warm client.
Optional per-run knobs (deny/allow tool sets, skill subset, model override,
exclusive tools, images, image attachments, the tool switch) are withheld when
empty and forwarded only to a backend whose ``run`` signature declares them.

A run with ``exclusive_tools`` (the deny-by-default concierge) is always served
by pydantic_ai (``_deny_by_default_backend``), never the owner's backend. So is
a run with ``force_pydantic_ai`` (the cloud's platform-funded free run), which
swaps the backend without the tool lockdown. Per-run usage limits
(``max_requests``, ``total_tokens_limit``) go only to a backend that declares
them; a limited run on any other backend is refused, never run unlimited.

BYOK: a turn given ``byok_api_key`` runs on a private backend built for that
turn alone (``AgentRouter.create_isolated_backend``), carrying the agent's own
ToolPolicy and released when the turn ends. Only pydantic_ai reads the
override, so ``spends_byok_key`` is the one answer to "will this turn spend the
caller's key"; the cloud's credit gate asks it before letting a turn skip the
wallet. If the private backend for a key-spending turn cannot be built, the
turn is refused with an ``error`` event rather than run on the platform's
credentials: the caller was let through as BYOK, so a silent fallback would be
free platform spend. A backend that ignores the key falls back to the shared
one, since that turn was gated and billed as platform anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

# Both guards moved to ``agents.backend`` at PA-7b — the router and the failover
# runner ask the same question now, and the answer is a fact about the backend
# protocol. Re-imported (not re-implemented) so ``AgentPool``'s own call sites
# below and the existing ``from pocketpaw.agents.pool import ...`` importers
# resolve to the ONE definition.
from pocketpaw.agents.backend import (
    ImageAttachment,
    _accepts_exclusive_tools_kwarg,
    _accepts_image_attachments_kwarg,
    _accepts_images_kwarg,
    _accepts_prompt_digest,
    _accepts_prompt_digest_kwarg,
    _accepts_run_limits_kwarg,
    _accepts_tools_enabled_kwarg,
    _accepts_turn_context_kwarg,
)
from pocketpaw.agents.errors import (
    AgentBackendUnavailable,
    AgentDisabled,
    AgentNotFound,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.prompt import AssembledPrompt, PromptContext, assemble, prompt_layer_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from pocketpaw.agents.backend import AgentBackend, LeasedClient, SessionHandle
    from pocketpaw.soul import SoulManager

logger = logging.getLogger(__name__)

#: What a BYOK turn says when its private backend could not be built.
BYOK_BACKEND_FAILED_MESSAGE = (
    "Your API key couldn't be used for this turn, so it wasn't run. "
    "Check the key in your workspace settings and try again."
)

#: The ``error`` a run carrying usage limits gets when the backend serving it
#: cannot take them. Refused rather than run unlimited.
RUN_LIMITS_UNSUPPORTED_MESSAGE = (
    "This reply couldn't start because its usage limits aren't supported here."
)


def spends_byok_key(backend: str | None, *, exclusive_tools: bool = False) -> bool:
    """Whether a turn served by ``backend`` spends the caller's BYOK override.

    Only pydantic_ai reads it (``byok_provider_api_key``, or the gateway
    settings), and a deny-by-default run (``exclusive_tools``) is always served
    by pydantic_ai. Any other backend built with the override still runs on the
    platform's credentials. A missing backend means ``claude_agent_sdk``, the
    same literal ``_build`` falls back to; legacy names are canonicalised the
    same way.
    """
    if exclusive_tools:
        return True
    from pocketpaw.agents.registry import _LEGACY_BACKENDS

    name = backend or "claude_agent_sdk"
    return _LEGACY_BACKENDS.get(name, name) == "pydantic_ai"


# The cloud path's prompt layers, in the order they are concatenated. The names
# resolve through ``prompt_layer_registry``; the ORDER is this caller's, because
# it is what makes the assembled text what a cloud agent expects to read.
#
# ``surface`` sits between the two (PA-2): who the agent is, then where the user
# is, then everything per-turn. That MOVES the preamble — it used to arrive
# inside ``knowledge_context``, i.e. below the authoritative instructions and
# inside the "Your Knowledge Base" wrapper that frames its content as reference
# data. Two consequences worth knowing about:
#   * the EE ``build_dynamic_context`` no longer prepends it, or it would render
#     twice;
#   * it now sits ABOVE ``ClaudeSDKBackend._VOLATILE_PROMPT_MARKERS``, so it
#     participates in the warm-client cache key. That is the point rather than a
#     side effect: a reused warm subprocess was keeping the preamble it launched
#     with, so the agent's prompt described the pocket as it looked when the
#     session started. It does mean a turn that changes the surface rebuilds the
#     subprocess — the same trade the home-pocket backend summary already makes.
#
# ``retrieval`` renders LAST (PA-3): stable first, volatile last. Extracting the
# per-message soul recall out of ``legacy_tail`` MOVED it — the bytes used to run
# ``instructions → recall → knowledge`` and now run ``instructions → knowledge →
# recall`` — because the tail still held ``instructions`` at the time. The move
# is deliberate and cost nothing measurable: both trailing blocks are per-message
# volatile, so their relative order cannot affect prompt caching,
# ``_behavior_prefix`` cuts at the earliest volatile marker and both orders open
# that region at the same offset (pinned in
# ``tests/test_prompt_retrieval_layer.py``), and the end of the prompt is the
# best-attended position — which memories retrieved for THIS question earn over
# a knowledge-base dump.
#
# ``instructions`` sits between ``surface`` and the tail (PA-4), and NOTHING
# MOVED. ``legacy_tail`` rendered instructions-then-knowledge; listing
# ``instructions`` immediately before it is the same concatenation. That is the
# whole reason this position and not another: PA-3 had a reason to move bytes and
# argued it, PA-4 has none, and a byte moved above ``_behavior_prefix``'s cut
# invalidates every warm Claude SDK client live at deploy.
#
# THE ORDER IS A CACHE CONTRACT, NOT A STYLE. Two properties make it one, and
# both are pinned in ``tests/test_prompt_instructions_layer.py`` rather than
# trusted to this comment:
#   * every KEYED layer must sit ABOVE the volatile region. ``_behavior_prefix``
#     cuts the warm-client key at the EARLIEST ``_VOLATILE_PROMPT_MARKERS`` match
#     (``min()`` across them), so a keyed layer ordered below one is cut out of
#     that key entirely — it would look keyed and behave unkeyed, losing its
#     cache contribution silently. ``instructions`` is keyed as of PA-4, so it
#     belongs above ``legacy_tail``'s ``## Your Knowledge Base`` marker, which is
#     exactly where byte-neutrality already put it.
#   * the prompt's two best-attended positions are its start and its end (the
#     U-curve). Identity takes the start; the memories retrieved for THIS
#     question take the end. The stable middle is where the material that must
#     be PRESENT but is not being attended to moment-by-moment belongs, and it is
#     also the region a prefix cache can actually reuse.
# ``atlas`` and ``user`` sit directly under ``identity`` (PA-5), and NOTHING
# MOVED for the same reason PA-4 moved nothing: neither has a producer yet, so
# both render to nothing and the assembler's empty-text skip leaves no gap. Their
# position is chosen for the day they do render — who the agent is, then what OS
# it runs inside, then who is talking, then where that person is looking. That is
# the volatility ladder the prefix cache wants (the primer changes on deploy, a
# member's block on a profile edit, the surface key on every navigation) and it
# keeps both above the volatile region, which a KEYED layer must be.
#
# What each will eventually carry is worth knowing before wiring it:
#   * ``atlas`` — the Paw OS primer, which today only the CHANNEL path builds
#     (``AgentContextBuilder._build_atlas_primer``). Handing it to the cloud path
#     adds ~1.5k chars to every cloud turn; that is a product call with a token
#     bill, not a consequence of adding a budget.
#   * ``user`` — the ``<about-member>`` block, which today reaches the prompt
#     INSIDE ``instructions`` (``build_behavior_instructions`` appends it). Moving
#     it here moves bytes above ``_behavior_prefix``'s cut AND changes
#     ``instructions``' key, which is a digest of exactly those bytes.
_SYSTEM_PROMPT_LAYERS = (
    "identity",
    "atlas",
    "user",
    "surface",
    "instructions",
    "legacy_tail",
    "retrieval",
)

# The per-turn layers: the knowledge-base wrapper (KB hits, <scope>,
# <participants>, <current-pocket>, the member briefing, <uploaded-files>) and
# the soul recall. Both are unkeyed and change every turn. A backend that
# declares ``turn_context`` gets them there instead of in ``system_prompt``: the
# Claude SDK applies its system prompt only at connect, so on a reused warm
# client these would otherwise stay whatever the connecting turn carried.
_TURN_CONTEXT_LAYERS = frozenset({"legacy_tail", "retrieval"})


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
    # The pydantic_ai backend deny-by-default runs (the public concierge) are
    # forced onto when ``backend`` is something else. Built on first use by
    # ``AgentPool._deny_by_default_backend``; stopped in ``_teardown``.
    deny_by_default_backend: Any = None
    # Set by ``invalidate`` on an instance dropped mid-run; the last run tears
    # it down.
    retired: bool = False


async def _release_backend(backend: Any) -> None:
    """Release a backend built for a single turn. Best-effort, never raises."""
    release = getattr(backend, "cleanup", None) or getattr(backend, "stop", None)
    if release is None:
        return
    try:
        await release()
    except Exception:
        logger.debug("releasing a per-turn backend failed", exc_info=True)


def _declares_param(func: Any, name: str) -> bool:
    """Does ``func`` name ``name`` in its signature (``**kwargs`` does not count)?"""
    import inspect

    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False


def _accepts_policy(backend_cls: type) -> bool:
    """Does this backend's ``__init__`` take a ``policy`` keyword?

    The pool builds the per-agent :class:`ToolPolicy` — it is the only place
    ``mcp_servers_allow`` is assembled from an agent's ``tools`` list — so a
    backend that cannot receive it runs under the process-wide policy instead.
    Asking the signature rather than checking a class means a new backend opts
    in by accepting the argument, not by being added to a list here.
    """
    import inspect

    try:
        return "policy" in inspect.signature(backend_cls.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False


class AgentPool:
    """Manages running agent instances with on-demand creation and idle eviction."""

    def __init__(self, max_idle: int = 300, max_instances: int = 20) -> None:
        self._instances: dict[str, AgentInstance] = {}
        self._max_idle = max_idle
        self._max_instances = max_instances
        self._gc_task: asyncio.Task | None = None
        self._build_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the GC background task. Idempotent.

        Two callers reach this on a cloud boot: ``dashboard_lifecycle``'s
        startup and the cloud lifespan hook, which only began running again when
        the dropped-``on_event`` bug was fixed. Without this guard the second
        call overwrites ``_gc_task`` and orphans the first loop, which then runs
        forever with nothing holding a handle to cancel it.
        """
        if self._gc_task is not None and not self._gc_task.done():
            return
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
        The dropped instance is torn down (its backend may hold a CLI
        subprocess): at once when idle, and by its last run's ``finally`` when a
        run is in flight, so a live stream is never aborted. The NEXT ``get``
        rebuilds (or raises ``AgentDisabled``).
        """
        instance = self._instances.pop(agent_id, None)
        if instance is None:
            return
        if instance.active_runs > 0:
            instance.retired = True
            return
        await self._teardown(instance)

    async def _assemble_system_prompt(
        self,
        instance: AgentInstance,
        *,
        agent_id: str,
        message: str,
        instructions: str,
        knowledge_context: str,
        system_message_override: str | None,
        surface_preamble: str = "",
        surface_cache_key: str | None = None,
        atlas_primer: str = "",
        tenant_scope: str | None = None,
        user_info: str = "",
        user_id: str | None = None,
        budget_chars: int | None = None,
        soul_recall: bool = True,
    ) -> AssembledPrompt:
        """Assemble the agent's system prompt from its layers.

        Returns the text AND a ``stable_digest`` over the layers that declared
        a cache key. The digest is what a backend caching an agent object folds
        into its own key: without it, an agent built on turn N with turn N's
        prompt baked in is handed back on turn N+1 to a different session (the
        bug PR #1842 fixed per-backend).

        The layers, in order:

        * ``identity`` — soul/persona identity (bootstrap ``identity`` + its
          ``# Key Knowledge``, falling back to the config persona /
          ``system_prompt``), then the entity-rooms A1
          ``system_message_override``, which SWAPS that base and keeps the
          layers below. KEYED, on the agent rather than on the rendered text.
        * ``atlas`` — the Paw OS primer. KEYED, on the tenant scope AND a digest
          of its bytes. Capped at 2000 chars, the first cap in the prompt that
          can bite. No producer yet: only the channel path builds this block,
          and it arrives here in PA-7.
        * ``user`` — the ``<about-member>`` block. KEYED, on the user id AND a
          digest of its bytes, because the id alone cannot see a profile EDIT
          and there is no working revision field to use instead. Capped at 500.
          No producer yet: the block reaches the prompt inside ``instructions``
          today, and relocating it moves bytes.
        * ``surface`` — the surface the user is looking at, resolved in the EE
          cloud layer and handed here as plain data. KEYED, on what the handler
          that built the preamble says it read (``None``, i.e. no key, on every
          path with no surface — OSS local runs, the channel adapters).
        * ``instructions`` — the authoritative behaviour rules (runtime identity,
          artifact delivery, the ripple LAW + delegation rule or, when an entity
          set one, the ``system_message_override`` that replaces them, plus the
          pocket-summary and about-member blocks). KEYED, on a digest of its own
          bytes — the one layer whose text is the complete artifact rather than a
          truncated view of a larger one, which is what makes hashing it an exact
          key here and a wrong one for ``surface`` and ``identity``.
        * ``legacy_tail`` — the knowledge-base wrapper. UNKEYED: its content is a
          per-message KB retrieval, so a key would move the digest every turn and
          destroy the cache it exists to protect. Until PA-4 it also carried the
          ``instructions``, so the most stable content in the prompt inherited
          the silence of the least.
        * ``retrieval`` — the per-message soul recall, keyed on nothing because
          it is keyed on the user's message. UNKEYED, and unlike the tail that
          is its PURPOSE rather than a limitation: it is the layer that makes
          "declares itself volatile" a thing a layer can do.

        Factored out of ``run`` (feat/claude-sdk-prewarm) so ``prewarm`` builds
        the IDENTICAL prompt the first real turn will. The Claude SDK warm-client
        cache key hashes the prompt's STABLE behavioral prefix (soul/persona +
        override + surface + ``instructions``); the volatile tail this also
        appends (``## Your Knowledge Base``, then the ``## Relevant Past
        Memories`` soul recall) is stripped before hashing, so a prewarm that
        passes ``message=""`` / ``knowledge_context=""`` still hashes to the SAME
        prefix as the run that passes the real values — which is exactly what
        makes the prewarmed client reused rather than evicted on turn 1. That
        contract survives PA-3's reorder: the cut takes the EARLIEST volatile
        marker, and swapping two blocks that are both below it does not move
        where the volatile region begins.

        ``budget_chars`` defaults to ``None`` — unbounded — and no caller sets
        it. That is not an oversight: ``context_builder``'s 32,000 is a CHANNEL
        number, and a cloud prompt's stable prefix alone has been measured past
        44k (``claude_sdk``'s volatile-marker note), so adopting it here would
        start dropping layers from live traffic. PA-9 measures the real one.
        """
        ctx = PromptContext(
            instance=instance,
            agent_id=agent_id,
            message=message,
            instructions=instructions,
            knowledge_context=knowledge_context,
            system_message_override=system_message_override,
            surface_preamble=surface_preamble,
            surface_cache_key=surface_cache_key,
            atlas_primer=atlas_primer,
            tenant_scope=tenant_scope,
            user_info=user_info,
            user_id=user_id,
            soul_recall=soul_recall,
        )
        layers = [prompt_layer_registry.get(name) for name in _SYSTEM_PROMPT_LAYERS]
        return await assemble(layers, ctx, budget_chars=budget_chars)

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
        exclusive_mcp_tools: bool = False,
        surface_preamble: str = "",
        surface_cache_key: str | None = None,
        model_override: str | None = None,
        tools_enabled: bool = True,
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

        It takes no history: the Claude SDK sends history in each turn's query
        text, so a prewarmed client needs none.
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
            assembled = await self._assemble_system_prompt(
                instance,
                agent_id=agent_id,
                message="",  # no turn yet — the volatile tail is stripped anyway
                instructions=instructions,
                knowledge_context="",  # stripped from the cache-key prefix
                system_message_override=system_message_override,
                # PA-2: the surface preamble is NOT part of the stripped tail —
                # it sits above the volatile markers now — so unlike ``message``
                # and ``knowledge_context`` it cannot be passed empty here. The
                # caller resolves the surface before firing this, and passing it
                # is what keeps the prewarmed client's prefix equal to turn 1's.
                surface_preamble=surface_preamble,
                surface_cache_key=surface_cache_key,
            )
        except Exception:
            logger.debug("prewarm: prompt assembly failed for %s (skipped)", agent_id)
            return

        # The backend's prewarm swallows ALL of its own errors, so this is
        # already safe; the outer guards above cover instance/prompt failures.
        # A backend that takes per-turn context separately is connected with the
        # stable layers only, exactly what its turns will send as system prompt.
        system_prompt = assembled.text
        if _accepts_turn_context_kwarg(getattr(instance.backend, "run", None)):
            system_prompt = assembled.split(_TURN_CONTEXT_LAYERS)[0]
        prewarm_kwargs: dict[str, Any] = {
            "session_key": session_key,
            "system_prompt": system_prompt,
        }
        # PA-6: the digest is now what the warm-client key hashes, so a prewarm
        # that withheld it would key under ``t:`` and turn 1 would key under
        # ``d:`` — the prewarmed subprocess EVICTED by the very turn it exists to
        # serve. Asked of the signature for the same reason ``_accepts_policy``
        # is: a backend opts in by taking the argument. Sent unconditionally
        # (never gated on truthiness) because turn 1 sends it unconditionally,
        # and the two keys have to be built from the same inputs.
        if _accepts_prompt_digest_kwarg(backend_prewarm):
            prewarm_kwargs["system_prompt_digest"] = assembled.stable_digest
        if deny_mcp_tool_ids:
            prewarm_kwargs["deny_mcp_tool_ids"] = deny_mcp_tool_ids
        if allow_sdk_tools:
            prewarm_kwargs["allow_sdk_tools"] = allow_sdk_tools
        if allow_mcp_tool_ids is not None:
            prewarm_kwargs["allow_mcp_tool_ids"] = allow_mcp_tool_ids
        if effective_skills:
            prewarm_kwargs["skill_names"] = effective_skills
        # Per-agent exclusive-tool cap (CX-2). Mirror ``run`` so the prewarmed
        # client's options (and cache key) match turn 1's — forwarded only when
        # True; the caller passes the agent's declared ids as ``allow_mcp_tool_ids``.
        if exclusive_mcp_tools:
            prewarm_kwargs["exclusive_mcp_tools"] = exclusive_mcp_tools
        # The turn's per-send model pick and tool switch change the model and
        # ``allowed_tools``, so the cache key. Withheld when default, and only to
        # a prewarm that declares them.
        if model_override and _declares_param(backend_prewarm, "model_override"):
            prewarm_kwargs["model_override"] = model_override
        if tools_enabled is False and _declares_param(backend_prewarm, "tools_enabled"):
            prewarm_kwargs["tools_enabled"] = False
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
        session_handle: SessionHandle | None = None,
        warm_client: LeasedClient | None = None,
        on_client_built: Callable[[Any, str, Callable], None] | None = None,
        model_override: str | None = None,
        exclusive_mcp_tools: bool = False,
        tools_enabled: bool = True,
        exclusive_tools: bool = False,
        image_attachments: Sequence[ImageAttachment] = (),
        surface_preamble: str = "",
        surface_cache_key: str | None = None,
        byok_api_key: str | None = None,
        byok_settings_override: dict[str, object] | None = None,
        images: tuple[tuple[bytes, str], ...] = (),
        force_pydantic_ai: bool = False,
        max_requests: int | None = None,
        total_tokens_limit: int | None = None,
        soul_recall: bool = True,
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

        ``warm_client`` / ``on_client_built`` (feat/warm-reuse WH-1) let the
        SessionSupervisor (WH-2/WH-3) drive the turn against a caller-LEASED warm
        client. They ride the SAME withhold-when-empty contract as the kwargs
        above — forwarded to the backend's ``run`` ONLY when set, so the 6
        non-Claude backends keep their narrower signature and only the Claude SDK
        backend acts on them. Both unset (the default) = the unchanged legacy
        warm-client path.

        ``model_override`` (CS-13) is the client's per-send model choice. It rides
        the SAME withhold-when-empty contract as the kwargs above — forwarded to the
        backend's ``run`` ONLY when set, so the 6 non-Claude backends keep their
        narrower signature and only the Claude SDK backend acts on it (where it wins
        over smart-routing / ``claude_sdk_model``). ``None`` = the unchanged path.

        ``surface_preamble`` / ``surface_cache_key`` (PA-2) are the surface the
        user is looking at: the rendered block (route, pocket snapshot, pinned
        widgets, live lists) and what the EE handler that built it says it read.
        They are NOT withhold-when-empty — they never reach a backend, they feed
        the ``surface`` prompt layer here. The key is threaded rather than
        derived from the text because the handler is the only thing that knows
        what it read: the pocket preamble shows the first 12 of N widgets under
        a 1500-char cap, so an edit to widget 13 changes the pocket and changes
        no rendered byte. ``""`` / ``None`` (the default) is the no-surface
        answer every non-cloud path gives, and leaves the layer keyless.

        ``exclusive_mcp_tools`` (CX-2) is the per-agent exclusive-tool signal. It
        rides the SAME withhold-when-empty contract: forwarded to the backend's
        ``run`` ONLY when ``True``, so the 6 non-Claude backends keep their narrower
        signature and only the Claude SDK backend acts on it (there it CAPS the MCP
        surface to ``allow_mcp_tool_ids`` alone — no universal grant). ``False``
        (the default) = the unchanged grant-union path.

        ``images`` are the pictures THIS turn should look at, already read into
        memory as (bytes, media_type). The caller reads them, because the caller
        is the layer that can prove a path belongs to the tenant asking; nothing
        below this line touches a filesystem on their behalf.

        ``byok_api_key`` is the caller's OWN provider key for this one turn
        (2026-08-28). It does NOT get forwarded to the cached backend — it
        REPLACES it: the run is served by a fresh, uncached backend built from
        the same settings with that key substituted.

        That is the whole point. This pool hands every session and surface the
        same cached instance, and a backend's provider credential is baked into
        the model object it builds. Passing one tenant's key to a shared backend
        would leave it there for the next tenant's turn, billing a stranger's
        account. An isolated instance cannot outlive the run that made it.

        ``byok_settings_override`` (2026-09-09) is what to substitute, when the
        answer is more than one key. A key for an OpenAI-compatible gateway also
        needs the gateway's address and model id, and those are as
        tenant-specific as the key itself. The cloud builds this dict; when it
        is absent the pool falls back to substituting ``byok_api_key`` alone,
        which is byte-identical to the 2026-08-28 behaviour.

        ``byok_api_key`` remains the GATE for both: no key, no isolated backend.

        None (the default) = the shared cached instance, unchanged for every
        existing run.

        ``force_pydantic_ai`` serves a non-BYOK run on pydantic_ai whatever the
        agent's backend is (the same backend ``_deny_by_default_backend`` builds,
        without ``exclusive_tools``), dropping a per-send model picked for the
        other backend. ``max_requests`` / ``total_tokens_limit`` cap the run; a
        backend that cannot take them refuses the turn. The cloud sets all three
        for a platform-funded free run. Defaults leave every run unchanged.

        ``soul_recall=False`` leaves the per-message soul recall out of the
        prompt (the cloud sets it on /sites, where each chat is about one site).
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
        assembled = await self._assemble_system_prompt(
            instance,
            agent_id=agent_id,
            message=message,
            instructions=instructions,
            knowledge_context=knowledge_context,
            system_message_override=system_message_override,
            surface_preamble=surface_preamble,
            surface_cache_key=surface_cache_key,
            soul_recall=soul_recall,
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
        # The backend serving THIS turn. A BYOK turn swaps in a private one built
        # for the turn alone, which the ``finally`` below releases.
        run_backend = instance.backend
        try:
            # Only forward the deny set when non-empty: the Claude SDK backend
            # accepts ``deny_mcp_tool_ids``, but the other backends keep the
            # narrower ``(message, *, system_prompt, history, session_key)``
            # signature, so passing the kwarg to them would raise TypeError.
            # The set is empty for every surface except /sites svelte-create
            # (which always runs on the Claude SDK backend), so this never
            # withholds a needed deny from a backend that would honor it.
            run_kwargs: dict[str, Any] = {
                "system_prompt": assembled.text,
                "history": history,
                "session_key": session_key,
            }
            # The prompt's stable digest (PA-1). NOT withhold-when-empty — it is
            # non-empty on every run — so the gate is the backend's SIGNATURE,
            # the same question ``_accepts_policy`` asks: a backend receives it
            # by declaring the parameter, not by being named in a list here.
            # As of PA-6 all four prompt-caching backends declare it and it is the
            # SOURCE of their cache keys rather than defence in depth:
            # ``pydantic_ai`` folds it into its agent key, ``deep_agents`` and
            # ``langchain_react`` into the compiled-graph key, and ``claude_sdk``
            # into the warm-client key in place of its behavioural prefix. A
            # backend that stops declaring it does not fail — it silently falls
            # back to hashing the prompt TEXT, which is #1842's trade (correct,
            # and a rebuild almost every turn).
            if _accepts_prompt_digest(type(instance.backend)):
                run_kwargs["system_prompt_digest"] = assembled.stable_digest
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
            # Native-resume handle (feat/session-supervisor SS-1). Same
            # withhold-when-empty rule as the kwargs above: only the Claude SDK
            # backend accepts ``session_handle`` (it passes a non-None
            # ``cli_session_id`` as ``ClaudeAgentOptions.resume`` and routes the
            # turn down the fresh-launch path); the other backends keep the
            # narrower signature, so the handle is forwarded ONLY when non-None.
            # None = legacy warm-client path, unchanged for every existing run.
            if session_handle is not None:
                run_kwargs["session_handle"] = session_handle
            # Leased warm-client seam (feat/warm-reuse WH-1). Same
            # withhold-when-empty rule: only the Claude SDK backend accepts
            # ``warm_client`` / ``on_client_built`` (it drives the turn against the
            # leased client or hands the supervisor a freshly-built one); the other
            # backends keep the narrower signature, so each is forwarded ONLY when
            # set. Both unset = the unchanged legacy warm-client path.
            if warm_client is not None:
                run_kwargs["warm_client"] = warm_client
            if on_client_built is not None:
                run_kwargs["on_client_built"] = on_client_built
            # Per-send model override (CS-13). Same withhold-when-empty rule as
            # the kwargs above: only the Claude SDK backend accepts
            # ``model_override``; the other backends keep the narrower signature,
            # so the override is forwarded ONLY when the client chose a model for
            # this turn. None = legacy path, unchanged for every existing run.
            if model_override is not None:
                run_kwargs["model_override"] = model_override
            # Per-agent exclusive-tool cap (CX-2). Same withhold-when-empty rule:
            # only the Claude SDK backend accepts ``exclusive_mcp_tools`` (it caps
            # the MCP surface to ``allow_mcp_tool_ids`` alone); the other backends
            # keep the narrower signature, so it is forwarded ONLY when True.
            # False = legacy grant-union path, unchanged for every existing run.
            if exclusive_mcp_tools:
                run_kwargs["exclusive_mcp_tools"] = exclusive_mcp_tools
            # Images for THIS turn, as (bytes, media_type). Forwarded further
            # down, once the run's real backend is known — and only to a backend
            # that DECLARES the parameter. Withhold-when-empty is not enough on
            # its own here: seven of the eight backends have a narrower
            # signature with no ``**kwargs``, and the Otherhand surface sends
            # images on every turn, so an unconditional forward would end each
            # of those turns in ``TypeError: run() got an unexpected keyword
            # argument 'images'``. That is the /sites failure this file already
            # records, one line above, with a different kwarg.
            #
            # ``image_attachments`` (the files the user attached, as
            # ``ImageAttachment``) is forwarded from the SAME place and for the
            # same reason. It is rarer than ``images`` — only a turn carrying an
            # attachment sends one — but rarity is not a guard: the moment a user
            # attaches a photo on an install running any of those seven backends,
            # an unguarded forward is a TypeError on their turn. Withholding
            # narrows WHEN, never WHERE; only the signature does that.
            # BYOK: swap the SHARED backend for a private one, built for this
            # run alone. If that build fails the turn is REFUSED, not run on the
            # shared backend: the cloud's credit gate let this turn skip the
            # wallet because the caller's key pays, so falling back to our
            # credentials would be unbilled platform spend.
            run_backend = instance.backend
            byok_build_failed = False
            if exclusive_tools:
                # A deny-by-default run (the public Paw Bar concierge) ALWAYS runs
                # on pydantic_ai, whatever backend the bound agent is configured
                # for (captain decision 2026-09-27). pydantic_ai is the one backend
                # that honours the whole lockdown: claude_sdk keeps its Read, Glob
                # and Grep built-ins, and every other backend rejects
                # ``exclusive_mcp_tools``. Fails closed: if no pydantic_ai backend
                # can be built the turn errors rather than falling back to the
                # owner's backend.
                try:
                    run_backend = self._deny_by_default_backend(
                        instance,
                        (byok_settings_override or {"byok_provider_api_key": byok_api_key})
                        if byok_api_key
                        else None,
                    )
                except Exception:
                    if not byok_api_key:
                        raise
                    logger.warning(
                        "BYOK: could not build an isolated pydantic_ai backend for %s; "
                        "refusing the turn",
                        agent_id,
                        exc_info=True,
                    )
                    byok_build_failed = True
                # A per-send model id was picked for the owner's backend; on a
                # different backend it is not a model we know to be valid.
                if run_backend is not instance.backend:
                    run_kwargs.pop("model_override", None)
            elif force_pydantic_ai and not byok_api_key:
                # Raises when no pydantic_ai backend can be built: the run must
                # not fall back to the agent's own backend.
                run_backend = self._deny_by_default_backend(instance, None)
                if run_backend is not instance.backend:
                    run_kwargs.pop("model_override", None)
            elif byok_api_key:
                from pocketpaw.agents.registry import _LEGACY_BACKENDS

                own_backend = instance.backend.settings.agent_backend
                try:
                    from pocketpaw.agents.router import AgentRouter

                    run_backend = AgentRouter.create_isolated_backend(
                        _LEGACY_BACKENDS.get(own_backend, own_backend),
                        instance.backend.settings,
                        settings_override=(
                            byok_settings_override or {"byok_provider_api_key": byok_api_key}
                        ),
                        # The agent's own ToolPolicy (planner opt-ins, narrowed
                        # profile), not the process-wide one a bare build gets.
                        policy=(
                            instance.backend.get_tool_policy()
                            if hasattr(instance.backend, "get_tool_policy")
                            else None
                        ),
                    )
                except Exception:
                    # Refuse only where the key would have paid. A backend that
                    # ignores the override was gated and billed as platform
                    # already, so the shared backend is the same turn.
                    byok_build_failed = spends_byok_key(own_backend)
                    logger.warning(
                        "BYOK: could not build an isolated backend for agent %s; %s",
                        agent_id,
                        "refusing the turn" if byok_build_failed else "using the shared backend",
                        exc_info=True,
                    )
            if byok_build_failed:
                yield AgentEvent(type="error", content=BYOK_BACKEND_FAILED_MESSAGE)
                return
            if max_requests is not None or total_tokens_limit is not None:
                if not _accepts_run_limits_kwarg(run_backend.run):
                    yield AgentEvent(type="error", content=RUN_LIMITS_UNSUPPORTED_MESSAGE)
                    return
                if max_requests is not None:
                    run_kwargs["max_requests"] = max_requests
                if total_tokens_limit is not None:
                    run_kwargs["total_tokens_limit"] = total_tokens_limit

            # Asked of the signature for the same reason ``_accepts_policy``
            # and ``_accepts_prompt_digest_kwarg`` are: a backend opts in by
            # taking the argument. Asked of ``run_backend`` rather than the
            # cached one because a BYOK run is served by a different object.
            if images and _accepts_images_kwarg(run_backend.run):
                run_kwargs["images"] = images
            if image_attachments and _accepts_image_attachments_kwarg(run_backend.run):
                run_kwargs["image_attachments"] = tuple(image_attachments)

            # Per-send tool switch (2026-09-11). TWO gates, and the first one
            # alone was a bug in production.
            #
            # Withhold-when-empty: the default True means "the tool surface this
            # run already resolved", which says nothing, so only an explicit
            # False is a request and only False rides. With the signature gate
            # below in place this half no longer prevents a crash — forwarding
            # True would be harmless, and a mutation that does so escapes the
            # suite on purpose. It stays because it keeps ``run_kwargs``
            # byte-identical on an ordinary turn, which is the shape every other
            # optional kwarg in this block has.
            #
            # AND ask the signature. Withholding narrows WHEN the kwarg is sent;
            # it never narrows WHERE. The moment a user turned the switch off,
            # that False went to whatever backend their agent runs on — a
            # logged-in workspace is on ``claude_agent_sdk`` — and every
            # tools-off turn died in ``TypeError: run() got an unexpected
            # keyword argument 'tools_enabled'``. Asked of ``run_backend``
            # rather than the cached one because a BYOK run is served by a
            # different object.
            if tools_enabled is False and _accepts_tools_enabled_kwarg(run_backend.run):
                run_kwargs["tools_enabled"] = tools_enabled
            # Deny-by-default surface (the public concierge). Same two gates:
            # only an explicit True rides, and only to a backend that declares
            # it. The run also carries ``exclusive_mcp_tools``, forwarded
            # unconditionally above, so a backend that declares neither fails
            # rather than running with its full tool set.
            if exclusive_tools and _accepts_exclusive_tools_kwarg(run_backend.run):
                run_kwargs["exclusive_tools"] = True
            # Per-turn layers ride ``turn_context`` for a backend that declares
            # it (see ``_TURN_CONTEXT_LAYERS``); the digest is unchanged, since
            # both layers are unkeyed. Asked of ``run_backend`` like the rest.
            if _accepts_turn_context_kwarg(run_backend.run):
                stable_text, turn_context = assembled.split(_TURN_CONTEXT_LAYERS)
                run_kwargs["system_prompt"] = stable_text
                if turn_context:
                    run_kwargs["turn_context"] = turn_context

            async for event in run_backend.run(message, **run_kwargs):
                instance.last_active = datetime.now(UTC)
                yield event
        finally:
            instance.active_runs -= 1
            instance.last_active = datetime.now(UTC)
            # A backend built for this turn alone (BYOK) is released here: a
            # Claude SDK backend otherwise keeps its CLI subprocess forever. The
            # instance's own and its cached deny-by-default backend are shared.
            if run_backend is not instance.backend and run_backend is not getattr(
                instance, "deny_by_default_backend", None
            ):
                await _release_backend(run_backend)
            # ``invalidate`` dropped this instance while it was busy; the last
            # run to finish tears it down.
            if getattr(instance, "retired", False) and instance.active_runs == 0:
                await self._teardown(instance)

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
        from pocketpaw.agents.registry import _LEGACY_BACKENDS, get_backend_class
        from pocketpaw.config import get_settings
        from pocketpaw.llm.providers.base import route_model
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS, ToolPolicy

        agent_id = str(agent_doc.id)
        config = agent_doc.config.model_dump()

        # Clone the cached settings (``Settings.load()`` re-parses config + env
        # on every build) and override with agent config. Deep copy: the cached
        # instance is shared process-wide and is mutated below.
        settings = get_settings().model_copy(deep=True)
        # The literal stays ``claude_agent_sdk`` on purpose, and is NOT the
        # cloud default (``pocketpaw_ee.cloud.agents.defaults``, which OSS core
        # cannot import anyway). ``AgentConfig`` carries a default, so
        # ``model_dump`` always includes the key and this fallback only fires
        # for a document written before the field existed — which is to say a
        # document from when ``claude_agent_sdk`` WAS the default. Answering
        # with today's default would silently re-home the oldest agents.
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
        # Hand the per-agent policy to any backend that takes one. This used
        # to name ClaudeSDKBackend outright, which meant every backend added
        # afterwards silently ran under the PROCESS-WIDE policy instead of the
        # agent's own — default profile ``full``, no narrowing — and its
        # ``mcp_servers_allow`` opt-ins were unreachable, since this is the only
        # place that set is built. Asking the signature keeps the guarantee with
        # the backend rather than with a list somebody has to remember to edit;
        # a backend whose ``__init__`` takes only ``settings`` is unaffected,
        # because passing ``policy=`` to one would raise TypeError.
        if _accepts_policy(backend_cls):
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

    @staticmethod
    def _deny_by_default_backend(instance: Any, byok_override: dict[str, Any] | None) -> Any:
        """The pydantic_ai backend a deny-by-default run is served by.

        The agent's own backend when it already IS pydantic_ai, so its model and
        its cached agent carry over. Otherwise a pydantic_ai backend built from
        the agent's settings with ``agent_backend`` switched, so its model is the
        deployment's ``pydantic_ai_model``: the owner's model id was chosen for a
        different backend and is not assumed to be valid here. That backend is
        cached on the instance (and stopped in ``_teardown``) so a concierge
        does not rebuild its tool bridge every turn. A built backend carries the
        agent's own ToolPolicy (planner opt-ins, narrowed profile), as the BYOK
        build below does, so a forced run keeps the tools the agent opted into.

        With BYOK the backend is built fresh for this run, like the BYOK path
        below, and a failed build raises: running the turn on the platform's
        pydantic_ai would spend our credentials on a turn the caller's key was
        meant to pay for. Also raises when no pydantic_ai backend can be built:
        the caller must not fall back to the owner's backend, which would drop
        the lockdown.
        """
        from pocketpaw.agents.registry import get_backend_class
        from pocketpaw.agents.router import AgentRouter

        pai_cls = get_backend_class("pydantic_ai")
        if pai_cls is None:
            raise RuntimeError(
                "A deny-by-default run needs the pydantic_ai backend, which is not installed"
            )
        own = instance.backend
        if isinstance(own, pai_cls) and byok_override is None:
            return own
        override = {"agent_backend": "pydantic_ai"}
        policy = own.get_tool_policy() if hasattr(own, "get_tool_policy") else None
        if byok_override is not None:
            return AgentRouter.create_isolated_backend(
                "pydantic_ai",
                own.settings,
                settings_override={**override, **byok_override},
                policy=policy,
            )
        cached = getattr(instance, "deny_by_default_backend", None)
        if cached is None:
            cached = AgentRouter.create_isolated_backend(
                "pydantic_ai", own.settings, settings_override=override, policy=policy
            )
            instance.deny_by_default_backend = cached
        return cached

    async def _teardown(self, instance: AgentInstance) -> None:
        """Gracefully shutdown an agent instance."""
        try:
            await instance.backend.stop()
        except Exception:
            pass
        # The pydantic_ai backend a deny-by-default run was forced onto holds
        # its own MCP exit stack, which only ``stop()`` releases.
        if instance.deny_by_default_backend is not None:
            try:
                await instance.deny_by_default_backend.stop()
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
    """Get or create the global agent pool.

    ``max_instances`` is resolved from settings HERE rather than as a constructor
    default so an explicit ``AgentPool(max_instances=...)`` — which every test uses
    — keeps winning, and so the env knob reaches the one instance the app actually
    runs on. The import is function-local for the same import-cycle reason as the
    other ``Settings.load()`` calls in this module.
    """
    global _pool
    if _pool is None:
        from pocketpaw.config import Settings

        _pool = AgentPool(max_instances=Settings.load().agent_pool_max_instances)
    return _pool
