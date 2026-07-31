"""Pydantic AI agent backend — in-process, dispatch-only.

Created 2026-07-29 (feat/pydantic-ai-backend). Backend #9 in
``_BACKEND_REGISTRY``. Implements the ``AgentBackend`` protocol on top of
``pydantic-ai-slim`` with an OpenAI-compatible model client pointed at the
self-hosted LiteLLM proxy.

Design source: ``docs/design/drafts/2026-07-29-pydantic-ai-agent-backend-prd.md``.

Why this exists: ``claude_agent_sdk`` spawns a Claude Code CLI Node subprocess
per concurrent run (~300-500 MB RSS). At the 300-400 concurrent-user target
that is ~75 GB of agent RSS before any other cost. This backend runs the agent
loop IN-PROCESS, so per-run cost is roughly the conversation context and the
binding constraint moves from process memory to the event loop — which is
addressed by adding web processes rather than boxes.

**Dispatch-only.** This backend emits tool calls; it does NOT execute local
file or shell work. Execution happens in Daytona / WebContainers / Tauri, S3,
or MCP servers. That is what collapses per-run memory AND what removes the
tenant-jail requirement: the per-run cwd jail (``agent_jail.resolve_agent_cwd``)
exists only on the ``claude_sdk`` chain, and PocketPaw's own
``tools/builtin/{shell,filesystem}.py`` jail against a PROCESS-GLOBAL
``file_jail_path``, so any in-process backend granted local fs/shell tools
would share one jail across every tenant. ``_POCKET_BLOCKED_TOOLS`` below is the
mechanical expression of that constraint, not a preference.

Two failure modes this file is deliberately shaped around:

1. **Per-run cancellation, never instance state.** ``AgentPool`` caches ONE
   backend instance per agent and drives concurrent runs through it. The
   sibling ``deep_agents`` backend keeps a single ``self._stop_flag``, so one
   run's ``stop()`` truncates every concurrent run and each new run's entry
   reset un-stops the others — observed 2026-07-29 in the load-test rig, where
   33 of 49 concurrent runs returned a clean ``stream_end`` carrying no
   content. Here each run owns a private ``_RunHandle``; ``stop()`` signals the
   handles that are live AT THAT MOMENT and a run starting afterwards gets a
   fresh one. The property is pinned by
   ``test_a_new_run_does_not_resurrect_a_stopped_one``, which was mutation-checked
   against a faithful shared-flag reproduction — note that the obvious
   "N concurrent runs all produce content" test does NOT catch the bug, because
   no ``stop()`` lands between those runs.

2. **Sync tools cap the whole process.** One blocking tool function runs on
   anyio's bounded worker thread pool, so a single sync tool throttles every
   concurrent run in the process. ``build_pydantic_ai_tools`` asserts every
   bridged tool is a coroutine.

**Prompt caching DOES survive this path — measured, not assumed.** This was the
design's open question 2, and the answer is yes, at least for
``litellm:deepseek-v3.2``. Six turns sharing one ~4.4k-token system prefix,
2026-07-29:

===========  ==========  ================  ==========
turn         cache read  uncached input    hit rate
===========  ==========  ================  ==========
0 (cold)              0  (full prompt)             0%
1                 4,416                19          100%
2                 4,416             9,316           32%
3                 4,416                19          100%
4                 8,000               943           89%
5                 4,416                19          100%
===========  ==========  ================  ==========

So a large stable SYSTEM PROMPT is cached upstream and read back without us
doing anything: ``deep_agents`` earns its margin by patching Anthropic
``cache_control`` markers into the request, and this route needs no equivalent
hook. The first turn is always a cold write and one turn in six missed, so
measure a WARM window when comparing rather than reading turn 1 alone.

**It does NOT appear to cover tool schemas, which is the load-bearing caveat.**
Same model, same day, with a SHORT system prompt and the full 49-tool bridged
surface attached: 13,509 input tokens and ``cache_read_tokens`` of **zero** on
every turn, repeatedly. Drop the tools and a 4.4k-token system prompt caches at
100%. So the cacheable thing here is the text prefix, not the tool definitions.

What that means for the ``/sites`` A/B: the pocket specialist's prompt is a
~12-17k-token design-rules block, which is exactly the shape that DOES cache, so
the target workload is likely fine. A chat agent carrying a small prompt and a
big tool surface is the case that will not cache and should be measured on its
own rather than assumed from the specialist's numbers.

The counts come from ``RunUsage``, which documents ``input_tokens`` as the
INCLUSIVE total with ``cache_read_tokens`` / ``cache_write_tokens`` as subsets
and normalizes providers (Anthropic, Bedrock) that report them disjointly — so
``_usage_event`` subtracts them back out to get the uncached remainder.

Verified live 2026-07-29 through a real LiteLLM proxy: a streamed turn, a tool
round-trip (tool actually executed, ``tool_use`` before ``tool_result``), and 8
concurrent runs on ONE cached instance with zero empty ``stream_end``.

Updated 2026-07-31 — ``run`` accepts the per-surface tool-gating kwargs
(``deny_mcp_tool_ids`` / ``allow_mcp_tool_ids`` / ``exclusive_mcp_tools``) and
HONOURS them; see ``_expand_tool_ids`` and ``_gate_mcp_toolsets``. Omitting them
crashed the run outright: ``AgentPool.run`` forwards each ONLY when a surface
sets it, so /chat (empty deny, no allow) worked while the first /sites turn died
with ``TypeError: run() got an unexpected keyword argument
'deny_mcp_tool_ids'``. Accepting them is only half the fix — they are a
surface's tool-REMOVAL controls, so swallowing them would have handed a
restricted surface the full tool set and reported success. The other six
non-Claude backends still carry the narrow signature and will crash the same way
on that surface.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import _DEFAULT_IDENTITY, BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)

# Providers whose wire format is OpenAI chat-completions. All of them are
# served by ``OpenAIChatModel`` + ``OpenAIProvider(base_url=...)``; only the
# base URL and key source differ.
_OPENAI_COMPATIBLE = frozenset({"litellm", "openai", "openai_compatible", "openrouter", "ollama"})

# Same gate as ``claude_sdk`` and ``deep_agents``: ``<pocket-scope>`` opens every
# pocket/site prompt. On a pocket session the agent's needs are fully covered by
# MCP tools, and leaving shell/fs attached has been observed sending the agent
# off to introspect its own environment (``env | grep pocket; curl localhost``).
# On THIS backend the filter is load-bearing rather than cosmetic — see the
# dispatch-only note in the module docstring.
_POCKET_SCOPE_SENTINEL = "<pocket-scope>"
_POCKET_BLOCKED_TOOLS = frozenset({"shell", "read_file", "write_file", "edit_file", "list_dir"})

# -- per-surface tool gating -------------------------------------------------
#
# A ``SurfaceProfile``'s deny/allow sets are written in the Claude SDK's
# vocabulary, because that is the backend they were built for: MCP tools spelled
# ``mcp__<server>__<tool>`` and bare built-in names like ``Bash``. NEITHER
# spelling exists here — pydantic-ai's ``PrefixedToolset`` names an MCP tool
# ``<server>_<tool>``, and there are no SDK built-ins at all. Comparing the raw
# strings therefore matches nothing, which is the dangerous failure: a surface
# that removed shell access would run with the full tool set and report success.


def _normalize_tool_id(tool_id: str) -> str:
    """``mcp__srv__do_thing`` -> ``srv_do_thing``. Other spellings pass through."""
    if tool_id.startswith("mcp__"):
        return tool_id.removeprefix("mcp__").replace("__", "_")
    return tool_id


# A surface's tool id -> the bridged PocketPaw tools that do the SAME JOB. A
# surface denying ``Bash`` is removing the ability to execute code on this box,
# not the seven letters; keeping ``shell`` because the name differs honours the
# letter of the deny and none of its point. The /code and /sites profiles deny
# the built-ins precisely because the agent runs on the BACKEND SERVER, and that
# hazard is identical here.
#
# The same holds for the in-process MCP ids, which reach this backend as bridged
# function tools rather than as ``mcp__`` ids: /sites svelte-create denies
# ``pocket_specialist__create`` so the agent CANNOT fall back to building a
# rippleSpec landing page (claude_sdk:1865 — "prose-only 'do not call the ripple
# tool' routing was proven to fail"), and ``create_pocket`` is that same
# capability under the OSS name.
#
# Deliberately over-inclusive where a capability spans several tools (``Bash``
# also takes ``run_python`` / ``install_package``): over-denial costs a
# capability, under-denial costs the boundary. Ids with no local equivalent
# (``WebSearch``, ``Skill``, ``sites_manager__create_landing_site``) simply have
# no row — normalization still covers them if they ever get bridged.
_SURFACE_TOOL_EQUIVALENTS: dict[str, frozenset[str]] = {
    "Bash": frozenset({"shell", "run_python", "install_package"}),
    "Read": frozenset({"read_file"}),
    "Write": frozenset({"write_file"}),
    "Edit": frozenset({"edit_file"}),
    "Glob": frozenset({"list_dir", "directory_tree"}),
    "Grep": frozenset({"list_dir", "directory_tree"}),
    "Agent": frozenset({"delegate_claude_code", "delegate_to_a2a_agent"}),
    "mcp__pocketpaw_pocket_specialist__create": frozenset({"create_pocket"}),
}


def _expand_tool_ids(tool_ids: frozenset[str]) -> frozenset[str]:
    """Translate a surface's tool ids into the names this backend uses."""
    out: set[str] = set()
    for raw in tool_ids:
        out.add(_normalize_tool_id(raw))
        out |= _SURFACE_TOOL_EQUIVALENTS.get(raw, frozenset())
    return frozenset(out)


class _RunHandle:
    """Private per-run cancellation state.

    One instance per ``run()`` invocation, held in the generator's own frame.
    This is the whole fix for failure mode 1 in the module docstring: because
    the flag lives here and not on the backend, a ``stop()`` for one run cannot
    truncate a sibling, and a run that starts after a ``stop()`` is not born
    already-cancelled.
    """

    __slots__ = ("stopped",)

    def __init__(self) -> None:
        self.stopped = False


class PydanticAIBackend:
    """Pydantic AI backend — in-process agent loop, dispatch-only tools."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="pydantic_ai",
            display_name="Pydantic AI",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            # Dispatch-only: this backend ships no built-in local file or shell
            # tools of its own. Everything it can call arrives through the tool
            # bridge under the active ToolPolicy.
            builtin_tools=[],
            tool_policy_map={},
            required_keys=[],
            supported_providers=[
                "litellm",
                "agentapi",
                "anthropic",
                "openai",
                "openai_compatible",
                "openrouter",
                "ollama",
            ],
            install_hint={
                "pip_package": "pydantic-ai-slim",
                "pip_spec": "pocketpaw[pydantic-ai]",
                "verify_import": "pydantic_ai",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sdk_available = False
        self._custom_tools: list | None = None
        self._mcp_tools: list | None = None
        # Holds every MCP server open for this instance's lifetime so the
        # refcount never returns to zero. Unwound in ``stop()``.
        self._mcp_stack: Any = None
        # ``_build_mcp_tools`` awaits while starting servers, so two concurrent
        # first runs would otherwise both see an empty cache and each spawn a
        # full set of subprocesses — the exact cost this guards against.
        self._mcp_lock = asyncio.Lock()
        self._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        # Live runs. A set, not a flag — see ``_RunHandle``.
        self._active: set[_RunHandle] = set()
        self._cached_agent: Any = None
        self._cached_agent_key: Any = None
        self._initialize()

    # -- policy -------------------------------------------------------------

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy
        self._custom_tools = None
        self._mcp_tools = None
        self._cached_agent = None
        self._cached_agent_key = None

    def _initialize(self) -> None:
        try:
            import pydantic_ai  # noqa: F401

            self._sdk_available = True
            logger.info("Pydantic AI SDK ready")
        except ImportError:
            logger.warning("Pydantic AI SDK not installed -- pip install 'pocketpaw[pydantic-ai]'")

    # -- model --------------------------------------------------------------

    def _parse_provider_model(self) -> tuple[str, str]:
        """Split ``pydantic_ai_model`` into ``(provider, model)``.

        Accepts ``provider:model`` or a bare model name, falling back to
        ``pydantic_ai_provider`` then ``llm_provider`` then ``litellm``.
        Mirrors ``DeepAgentsBackend._parse_provider_model`` so an operator can
        move a value between the two settings without reformatting it.
        """
        model_str = (self.settings.pydantic_ai_model or "").strip()
        if ":" in model_str:
            provider, _, model = model_str.partition(":")
            return provider.strip(), model.strip()

        provider = getattr(self.settings, "pydantic_ai_provider", "auto")
        if provider == "auto":
            provider = self.settings.llm_provider
        if provider == "auto":
            provider = "litellm"
        return provider, model_str

    def _build_model(self) -> Any:
        """Build the pydantic-ai model client for the configured provider."""
        provider, model = self._parse_provider_model()

        if provider == "agentapi":
            # Development path: borrow a local CLI's own authentication instead
            # of a provider key. Text only — the wrapped agent never emits
            # structured tool calls, so the tool loop is inert. See
            # pydantic_ai_agentapi for the full caveat.
            from pocketpaw.agents.pydantic_ai_agentapi import AgentAPIModel

            return AgentAPIModel(
                model or "claude",
                base_url=str(getattr(self.settings, "agentapi_base_url", "") or ""),
                timeout=float(getattr(self.settings, "agentapi_timeout", 0) or 600),
            )

        if provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(
                model or "claude-sonnet-4-6",
                provider=AnthropicProvider(api_key=self.settings.anthropic_api_key or ""),
            )

        if provider not in _OPENAI_COMPATIBLE:
            raise ValueError(
                f"pydantic_ai backend: unsupported provider {provider!r}. "
                f"Supported: {', '.join(sorted(_OPENAI_COMPATIBLE | {'anthropic', 'agentapi'}))}."
            )

        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        base_url, api_key, model = self._resolve_openai_compatible(provider, model)
        logger.info(
            "Pydantic AI: OpenAIChatModel(%r) via provider=%s base_url=%s",
            model,
            provider,
            base_url,
        )
        return OpenAIChatModel(
            model,
            provider=OpenAIProvider(base_url=base_url, api_key=api_key),
        )

    def _resolve_openai_compatible(self, provider: str, model: str) -> tuple[str | None, str, str]:
        """Return ``(base_url, api_key, model)`` for an OpenAI-compatible provider."""
        if provider == "litellm":
            # NOTE the ``/v1``. ``deep_agents`` passes ``litellm_api_base`` WITHOUT
            # it because ChatLiteLLM hands the URL to the LiteLLM SDK, which
            # appends the path itself. Here the OpenAI client appends only
            # ``/chat/completions``, so the version segment has to be on the base
            # URL or every request 404s. Same setting, two different contracts —
            # this is the single easiest thing to get wrong in this file.
            base = (self.settings.litellm_api_base or "http://localhost:4000").rstrip("/")
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            # The proxy is the auth boundary: this is the tenant's virtual key,
            # not an upstream provider key. A placeholder keeps the OpenAI client
            # happy on proxies configured without auth.
            return (
                base,
                self.settings.litellm_api_key or "not-needed",
                (model or self.settings.litellm_model or ""),
            )

        if provider == "openai":
            return (
                None,
                self.settings.openai_api_key or "",
                (model or self.settings.openai_model or "gpt-5.2"),
            )

        if provider == "openrouter":
            return (
                "https://openrouter.ai/api/v1",
                self.settings.openrouter_api_key or self.settings.openai_compatible_api_key or "",
                model or self.settings.openrouter_model or "",
            )

        if provider == "ollama":
            host = (self.settings.ollama_host or "http://localhost:11434").rstrip("/")
            if not host.endswith("/v1"):
                host = f"{host}/v1"
            # Ollama's OpenAI-compatible endpoint ignores the key but the client
            # requires a non-empty one.
            return host, "ollama", (model or self.settings.ollama_model or "llama3.2")

        # openai_compatible
        base = (self.settings.openai_compatible_base_url or "").rstrip("/") or None
        return (
            base,
            self.settings.openai_compatible_api_key or "",
            (model or self.settings.openai_compatible_model or ""),
        )

    # -- tools --------------------------------------------------------------

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw tools as pydantic-ai ``Tool`` objects.

        Early-returns when ``_custom_tools`` is already populated. That guard is
        load-bearing, not an optimisation: ``attach_specialist_tools`` pre-fills
        the list for an isolated specialist run, and returning here is what keeps
        ``pocket_specialist__create`` — auto-injected by the bridge for every
        main-agent run — OUT of the specialist's own backend. Without it the
        specialist can call itself. (``deep_agents._build_custom_tools`` carries
        the same guard for the same reason.)
        """
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_pydantic_ai_tools

            self._custom_tools = build_pydantic_ai_tools(
                self.settings, backend="pydantic_ai", policy=self._policy
            )
        except Exception as exc:
            logger.info("Could not build custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    async def _build_mcp_tools(self) -> list:
        """Build pydantic-ai toolsets from PocketPaw's configured MCP servers.

        Two separate things keep this off the request path, and BOTH are
        required — instance caching alone is not enough:

        1. **Built once per instance.** Constructing servers per run would put a
           process spawn in every turn.
        2. **Held open for the instance's lifetime.** pydantic-ai's MCP servers
           are refcounted (``mcp.py:_running_count``): a shared server tears down
           the moment concurrent runs reach zero and RESPAWNS on the next run. A
           cached-but-unheld server therefore still spawns a stdio subprocess per
           run whenever traffic is sparse — which is most of the time outside a
           load test. Entering each server once into ``self._mcp_stack`` pins the
           refcount at >= 1, so per-run enter/exit can never drop it to zero.
           ``stop()`` unwinds the stack.

        ``test_mcp_servers_spawn_once_across_many_runs`` measures exactly that
        and is mutation-checked: drop the exit-stack hold and the spawn count
        goes from 1 to one-per-run.
        """
        if self._mcp_tools is not None:
            return self._mcp_tools

        async with self._mcp_lock:
            # Re-check: a concurrent first run may have built it while we waited.
            if self._mcp_tools is None:
                self._mcp_tools = await self._start_mcp_servers()
            return self._mcp_tools

    @staticmethod
    def _mcp_client_for(cfg: Any) -> Any:
        """Build the fastmcp transport for one PocketPaw MCP server config.

        ``MCPToolset`` takes anything fastmcp can build a transport from. Stdio
        needs an explicit ``StdioTransport`` so ``keep_alive`` is set on purpose
        rather than inherited: it keeps the child process alive across client
        sessions, which is the second half of not spawning per run (the first
        being the exit-stack hold in the caller).
        """
        transport = getattr(cfg, "transport", "")
        if transport == "stdio" and getattr(cfg, "command", None):
            from fastmcp.client.transports import StdioTransport

            return StdioTransport(
                command=cfg.command,
                args=list(cfg.args or []),
                env=cfg.env or None,
                keep_alive=True,
            )
        if transport in ("sse", "http", "streamable-http") and getattr(cfg, "url", None):
            # fastmcp infers SSE vs streamable-HTTP from the URL.
            return cfg.url
        return None

    async def _start_mcp_servers(self) -> list:
        """Construct and start the configured MCP servers. Caller holds the lock."""
        if not getattr(self.settings, "pydantic_ai_mcp_enabled", True):
            return []

        try:
            from pydantic_ai.mcp import MCPToolset
            from pydantic_ai.toolsets import PrefixedToolset
        except ImportError:
            logger.debug("pydantic-ai MCP extra not installed, skipping MCP tools")
            return []

        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            return []

        servers: list = []
        for cfg in load_mcp_config() or []:
            if not cfg.enabled:
                continue
            if not self._policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue
            try:
                client = self._mcp_client_for(cfg)
                if client is None:
                    continue
                # Prefix with the server name so tools from two servers can't
                # collide, matching what ``load_mcp_toolsets`` does for the
                # config-file path.
                servers.append(PrefixedToolset(MCPToolset(client), cfg.name))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping MCP server '%s': %s", cfg.name, exc)

        # Pin the refcount. Without this every server is torn down as soon as
        # concurrent runs reach zero and respawned on the next turn — see the
        # docstring. A server that fails to start is dropped rather than
        # failing the run: MCP is additive to the tool surface, never
        # load-bearing.
        if servers:
            from contextlib import AsyncExitStack

            stack = AsyncExitStack()
            held: list = []
            for server in servers:
                try:
                    await stack.enter_async_context(server)
                    held.append(server)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MCP server %r failed to start; continuing without it: %s",
                        getattr(server, "tool_prefix", server),
                        exc,
                    )
            if held:
                self._mcp_stack = stack
            else:
                await stack.aclose()
            servers = held

        if servers:
            logger.info(
                "Built %d MCP toolsets for Pydantic AI, held open for the instance lifetime",
                len(servers),
            )
        return servers

    def attach_specialist_tools(self, tools: list[Any]) -> None:
        """Merge specialist-internal tools into the cache for an isolated run.

        Implementing this is what makes ``pydantic_ai`` eligible for
        ``pocket_specialist_backend`` at all — ``AgentBackend`` excludes any
        backend whose ``attach_specialist_tools`` raises (``backend.py:218``).

        Also pre-sets ``_mcp_tools = []`` to short-circuit MCP loading:
        specialist runs are short-lived and need only the tools passed here, so
        spinning up the user's full MCP server set would add startup latency and
        a hang risk for no benefit.

        Each call EXTENDS the list; tools are not deduplicated. Use an isolated
        backend instance (``AgentRouter.create_isolated_backend``) so tools don't
        accumulate across specialist runs.
        """
        if self._custom_tools is None:
            self._custom_tools = []
        self._custom_tools.extend(tools)
        self._mcp_tools = []
        self._cached_agent = None
        self._cached_agent_key = None

    def attach_subprocess_env(self, env: dict[str, str]) -> None:  # noqa: ARG002
        """No-op — this backend spawns no subprocess.

        Part of the ``AgentBackend`` contract that is subprocess-shaped. An
        in-process backend has nothing to inject into, and per-request tenancy
        reaches it through ContextVars instead.
        """
        return None

    def _build_capabilities(self, skill_names: frozenset[str] = frozenset()) -> list:
        """Build the ``pydantic-ai-harness`` capabilities for this backend.

        Four of the PRD's six are wired. The other two are dropped, with the
        reason recorded here rather than left as a silent gap — the PRD's own
        done-condition allows dropping a capability that assumes the FileSystem
        or Shell this design excludes.

        **Wired:**

        * ``SlidingWindow`` + ``ClearToolResults`` (Compaction) — a long tool
          loop is exactly what blows the context on a dispatch-only agent, and
          neither strategy touches disk. ``DeduplicateFileReads`` is NOT used:
          it keys off file-read tools this backend does not have.
        * ``Planning`` — a todo toolset, no filesystem.
        * ``OverflowingToolOutput`` — the per-tool ceiling, enforced in the
          harness rather than only in our bridge wrapper. ``Truncate``, not
          ``Spill``: spilling writes overflow to a store on disk, and a
          process-global path is shared across tenants here.
        * ``StepPersistence`` with an in-memory store — ``FileStepStore`` and
          ``SqliteStepStore`` are both disk-backed. In-memory keeps the run
          record available for the turn without a shared-path write.

        * **Skills** — via ``pydantic-ai-skills`` (``SkillsCapability``), NOT
          the harness, which ships no skills capability of its own in 0.8.0.
          See ``_build_skills_capability``.

        **Dropped:**

        * **Subagents** — ``SubAgents`` defaults to discovering agents from an
          ``agents`` FOLDER on disk, and we have no in-code subagents to
          register. Wiring it with an empty list would add a capability that
          can never fire. Revisit when there is a real subagent to declare.
        """
        if not getattr(self.settings, "pydantic_ai_harness_enabled", True):
            return []
        try:
            from pydantic_ai_harness.compaction import ClearToolResults, SlidingWindow
            from pydantic_ai_harness.overflowing_tool_output import (
                Band,
                OverflowingToolOutput,
                Truncate,
            )
            from pydantic_ai_harness.planning import Planning
            from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence
        except ImportError:
            logger.debug("pydantic-ai-harness not installed, running without capabilities")
            return []

        limit = int(getattr(self.settings, "pydantic_ai_max_tool_output_chars", 0) or 0)
        capabilities: list = [
            SlidingWindow(max_messages=self.settings.pydantic_ai_compaction_max_messages),
            ClearToolResults(max_messages=self.settings.pydantic_ai_compaction_max_messages),
            Planning(),
            StepPersistence(store=InMemoryStepStore(), agent_name="pocketpaw"),
        ]
        if limit:
            capabilities.append(
                OverflowingToolOutput(bands=[Band(over=limit, action=Truncate(max_chars=limit))])
            )

        skills = self._build_skills_capability(skill_names)
        if skills is not None:
            capabilities.append(skills)
        return capabilities

    def _build_skills_capability(self, skill_names: frozenset[str] = frozenset()) -> Any:
        """Expose PocketPaw's skills through ``pydantic-ai-skills``.

        Skills reach the model by progressive disclosure: the agent sees a list
        of names and descriptions, and pulls a skill's full body only when it
        decides to use one. That matters here because the alternative — pasting
        every skill into the system prompt — is what makes the prompt enormous
        on a backend whose per-run cost IS the context.

        Three deliberate constraints, and the first is the one measured:

        * **Source is PocketPaw's BUNDLED skills, not the machine's skill
          directories.** ``SkillLoader``'s default ``SKILL_PATHS`` scan
          ``~/.agents/skills``, ``~/.claude/skills`` and
          ``~/.pocketpaw/skills`` — the OPERATOR's own skills. In a
          multi-tenant process those are not tenant content and have no
          business in a tenant's agent, and the cost scales with whatever the
          operator happens to have installed.

          Measured live 2026-07-29 against ``litellm:deepseek-v3.2``, input
          tokens for one trivial turn:

          ===========================================  =============
          configuration                                input tokens
          ===========================================  =============
          harness off, skills off                                 13
          harness on, skills off                                 833
          harness on + 19 BUNDLED skills (shipped)             5,784
          harness on + 42 skills from ``~/.claude``            8,644
          ===========================================  =============

          Progressive disclosure is doing its job — inlining the 42 would have
          been ~118k tokens. The prefix does get cached upstream (see the module
          docstring), so a warm turn pays little of this; the cold turn and any
          cache miss pay all of it. Hence: ship the product's own skills, which
          is a bounded and intentional set, and let a caller narrow further with
          ``skill_names``.
        * **Skills are passed programmatically, never discovered by the
          capability.** ``SkillsCapability`` can scan directories, clone git
          repos, or read S3. All three are declined: PocketPaw owns skill
          discovery, and a second mechanism is a second place a tenant's
          surface could differ from what the policy says it is.
        * **``run_skill_script`` is excluded.** It executes a skill's bundled
          script — local execution, which dispatch-only rules out and which has
          no per-tenant jail on an in-process backend. ``read_skill_resource``
          goes too, since we pass no resources, so leaving it would advertise a
          tool that can only fail.

        *only* is the subset named by ``skill_names`` when the caller supplies
        one — the same per-entity kwarg the Claude SDK backend uses to narrow a
        run's skills.

        Returns ``None`` when the package is absent, the feature is off, or no
        skills resolve — an empty capability would just add tool surface.
        """
        if not getattr(self.settings, "pydantic_ai_skills_enabled", True):
            return None
        try:
            from pydantic_ai_skills import Skill as PaiSkill
            from pydantic_ai_skills import SkillsCapability
        except ImportError:
            logger.debug("pydantic-ai-skills not installed, running without skills")
            return None

        try:
            loaded = self._load_bundled_skills()
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not load PocketPaw skills: %s", exc)
            return None

        skills = [
            PaiSkill(name=s.name, description=s.description, content=s.content)
            for s in loaded
            if not getattr(s, "disable_model_invocation", False)
            and (not skill_names or s.name in skill_names)
        ]
        if not skills:
            return None

        logger.info("Pydantic AI: exposing %d PocketPaw skills", len(skills))
        return SkillsCapability(
            skills=skills,
            exclude_tools={"run_skill_script", "read_skill_resource"},
            validate=False,
        )

    @staticmethod
    def _load_bundled_skills() -> list:
        """Load ONLY the skills PocketPaw ships, ignoring the machine's dirs.

        ``SkillLoader`` is reused for the parsing, but pointed exclusively at
        the package's own ``_bundled/skills`` tree by clearing the default
        ``SKILL_PATHS``. See ``_build_skills_capability`` for why the operator's
        home directories are deliberately not a source.
        """
        from pathlib import Path

        import pocketpaw.bundled_skills as bundled_pkg
        from pocketpaw.skills.loader import SkillLoader

        bundled_dir = Path(bundled_pkg.__file__).parent / "_bundled" / "skills"
        if not bundled_dir.is_dir():
            return []
        loader = SkillLoader()
        loader.paths = [bundled_dir]  # NOT extra_paths — replaces the home dirs
        return list(loader.load(force=True).values())

    # -- agent assembly -----------------------------------------------------

    @staticmethod
    def _gate_mcp_toolsets(
        mcp_toolsets: list,
        deny: frozenset[str],
        allow_mcp_tool_ids: frozenset[str] | None,
        exclusive_mcp_tools: bool,
    ) -> list:
        """Apply the surface's deny / allow sets to the MCP toolsets.

        Mirrors ``claude_sdk``'s precedence: deny is subtracted first and is the
        hard boundary, then the RESTRICTIVE allow set keeps only what it names.

        The allow set is applied to MCP toolsets ONLY, matching the SDK, where
        "built-in SDK tools are NEVER filtered here — only ``mcp__*`` ids". The
        split lands differently on this backend but in the same place: our MCP
        toolsets are the user's EXTERNAL configured servers, while the
        in-process pocket / widget / sites tools arrive as bridged function
        tools — which is exactly the group the SDK's grant unions back in
        (``POCKET_CREATION_GRANT`` / widget / atlas ids). Restricting them here
        would be stricter than the surface asks for and would break /sites.
        """
        if not mcp_toolsets or (not deny and allow_mcp_tool_ids is None):
            return mcp_toolsets

        # An exclusive turn CAPS the surface to the allow set alone — with no
        # allow set that is an EMPTY permitted set, so every MCP tool goes. That
        # is how a dedicated agent wins over a broad surface (claude_sdk CX-1).
        permitted = (
            (allow_mcp_tool_ids or frozenset()) if exclusive_mcp_tools else allow_mcp_tool_ids
        )
        allowed = None if permitted is None else _expand_tool_ids(permitted)

        def _keep(_ctx: Any, tool_def: Any) -> bool:
            name = getattr(tool_def, "name", "")
            if name in deny:
                return False
            return allowed is None or name in allowed

        return [ts.filtered(_keep) for ts in mcp_toolsets]

    def _get_or_create_agent(
        self,
        model: Any,
        instructions: str,
        mcp_toolsets: list,
        skill_names: frozenset[str] = frozenset(),
        *,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        exclusive_mcp_tools: bool = False,
    ) -> Any:
        """Build (and cache) the pydantic-ai ``Agent``.

        Cached on everything that shapes the tool surface or the model, so
        flipping between pocket and non-pocket sessions on one instance rebuilds
        rather than silently reusing the wrong tool set.
        """
        from pydantic_ai import Agent

        is_pocket_session = _POCKET_SCOPE_SENTINEL in (instructions or "")
        deny = _expand_tool_ids(deny_mcp_tool_ids)

        # Build tools BEFORE the cache key. ``_build_custom_tools`` populates
        # ``self._custom_tools`` on first call, so keying off it beforehand
        # compares ``id(None)`` against ``id(list)`` on the next run and the
        # cache NEVER hits — every run re-instantiates the whole tool set. That
        # is not a slow path, it is a per-run cost on the thing whose entire
        # purpose is a low per-run cost, and it is invisible except as latency.
        tools = list(self._build_custom_tools())

        agent_key = (
            self.settings.pydantic_ai_model,
            is_pocket_session,
            len(mcp_toolsets),
            id(self._custom_tools),
            len(tools),
            # In the key, not just a constructor argument: the skill subset
            # shapes the agent's capabilities, so an entity with a narrower set
            # must not be served an agent cached for a wider one.
            tuple(sorted(skill_names)),
            # Same reason, and here it decides a security boundary rather than a
            # capability: ``AgentPool`` drives EVERY surface through one cached
            # instance, so without these in the key whichever surface ran first
            # would pick the tool surface for all of them — a restricted turn
            # would silently be served the unrestricted agent.
            tuple(sorted(deny)),
            None if allow_mcp_tool_ids is None else tuple(sorted(allow_mcp_tool_ids)),
            exclusive_mcp_tools,
        )
        if self._cached_agent is not None and self._cached_agent_key == agent_key:
            return self._cached_agent

        if deny:
            before = len(tools)
            tools = [t for t in tools if getattr(t, "name", "") not in deny]
            if before != len(tools):
                logger.info(
                    "Surface tool-deny: stripped %d tool(s) for %s",
                    before - len(tools),
                    sorted(deny_mcp_tool_ids),
                )

        mcp_toolsets = self._gate_mcp_toolsets(
            mcp_toolsets, deny, allow_mcp_tool_ids, exclusive_mcp_tools
        )

        if is_pocket_session:
            before = len(tools)
            tools = [t for t in tools if getattr(t, "name", "") not in _POCKET_BLOCKED_TOOLS]
            if before != len(tools):
                logger.info(
                    "Pocket session — stripped %d shell/fs tools from agent",
                    before - len(tools),
                )

        agent = Agent(
            model,
            instructions=instructions,
            tools=tools,
            toolsets=list(mcp_toolsets) or None,
            capabilities=self._build_capabilities(skill_names) or None,
            # The agent is shared across concurrent runs; conversation state
            # rides in ``message_history`` per run, never on the agent.
            retries=2,
        )
        self._cached_agent = agent
        self._cached_agent_key = agent_key
        return agent

    def _build_history(self, history: list[dict] | None) -> list:
        """Convert PocketPaw's ``[{role, content}]`` history to pydantic-ai messages."""
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            TextPart,
            UserPromptPart,
        )

        messages: list = []
        for msg in history or []:
            content = msg.get("content") or ""
            if not content:
                continue
            if msg.get("role") == "assistant":
                messages.append(ModelResponse(parts=[TextPart(content=content)]))
            else:
                messages.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        return messages

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,  # noqa: ARG002
        # Per-entity skill subset. Rides the withhold-when-empty contract:
        # ``AgentPool.run`` forwards it only when non-empty, so an empty set
        # means "no per-entity narrowing" and every bundled skill is offered.
        skill_names: frozenset[str] = frozenset(),
        # -- per-surface tool gating (see ``_gate_mcp_toolsets``) ------------
        # These ride the same withhold-when-empty contract, which is why their
        # absence was invisible: the pool forwards them ONLY when a surface
        # actually sets one, so every test and every /chat turn passed and the
        # first /sites turn died with ``TypeError: run() got an unexpected
        # keyword argument 'deny_mcp_tool_ids'`` (observed live 2026-07-31).
        # ``test_run_accepts_every_kwarg_the_pool_forwards`` reads the pool's
        # real forwarding table so the next kwarg fails a test, not a run.
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        exclusive_mcp_tools: bool = False,
        # Accepted and deliberately unused — each is Claude-SDK plumbing with no
        # analogue here, and each is safe to drop:
        #   ``allow_sdk_tools``   ADDITIVE grant of SDK built-ins. There are no
        #                         SDK built-ins on this backend, so there is
        #                         nothing to grant; ignoring it removes tools,
        #                         never adds them.
        #   ``model_override``    per-send model choice, consumed only by the
        #                         Claude SDK backend (as on the other six).
        #   ``session_handle`` /  native CLI-session resume and warm-client
        #   ``warm_client`` /     reuse — this backend has no subprocess to
        #   ``on_client_built``   resume or lease.
        allow_sdk_tools: frozenset[str] = frozenset(),  # noqa: ARG002
        model_override: str | None = None,  # noqa: ARG002
        session_handle: Any = None,  # noqa: ARG002
        warm_client: Any = None,  # noqa: ARG002
        on_client_built: Any = None,  # noqa: ARG002
    ) -> AsyncIterator[AgentEvent]:
        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Pydantic AI SDK not installed.\n\n"
                    "Install with: pip install 'pocketpaw[pydantic-ai]'"
                ),
            )
            return

        # Per-run cancellation. Registered so ``stop()`` can reach it, but owned
        # by this frame so no sibling run can flip it. See ``_RunHandle``.
        handle = _RunHandle()
        self._active.add(handle)

        # Tool ids already announced, so the early PartStartEvent signal and the
        # authoritative FunctionToolCallEvent don't double-announce one call.
        announced: set[str] = set()

        try:
            model = self._build_model()
            instructions = system_prompt or _DEFAULT_IDENTITY
            mcp_toolsets = await self._build_mcp_tools()
            agent = self._get_or_create_agent(
                model,
                instructions,
                mcp_toolsets,
                skill_names,
                deny_mcp_tool_ids=deny_mcp_tool_ids,
                allow_mcp_tool_ids=allow_mcp_tool_ids,
                exclusive_mcp_tools=exclusive_mcp_tools,
            )

            kwargs: dict[str, Any] = {"message_history": self._build_history(history)}
            max_turns = self.settings.pydantic_ai_max_turns
            if max_turns and max_turns > 0:
                from pydantic_ai.usage import UsageLimits

                kwargs["usage_limits"] = UsageLimits(request_limit=max_turns)

            async with agent.run_stream_events(message, **kwargs) as stream:
                async for event in stream:
                    if handle.stopped:
                        break
                    for agent_event in self._map_event(event, announced):
                        yield agent_event

        except asyncio.CancelledError:
            # Caller cancelled this run specifically — the correct per-run
            # cancellation path. Propagate; do not degrade it into "done".
            raise
        except Exception as exc:
            logger.error("Pydantic AI streaming error: %s", exc, exc_info=True)
            yield AgentEvent(type="error", content=self._explain_error(exc))
            yield AgentEvent(type="done", content="")
            return
        finally:
            self._active.discard(handle)

        yield AgentEvent(type="done", content="")

    def _explain_error(self, exc: Exception) -> str:
        """Turn a provider error into something that names the actual problem.

        A proxy auth failure is the single most confusing error on this path,
        because there are TWO credentials and the raw body implicates neither.
        The virtual key authenticated fine — the request was routed, a model
        group was chosen, fallbacks were attempted — and then the PROXY's own
        upstream credential was rejected. The unhelpful default reading is
        "my key is wrong", which sends you to change the one thing that works.

        This cost real time to diagnose by hand, so the backend says it now.
        """
        text = str(exc)
        raw = f"Pydantic AI error: {text}"

        is_auth = "status_code: 401" in text or "authentication_error" in text
        provider, model = self._parse_provider_model()
        if not (is_auth and provider == "litellm"):
            return raw

        base = (self.settings.litellm_api_base or "").rstrip("/")
        return (
            f"The LiteLLM proxy rejected model {model!r} with a 401 from its UPSTREAM "
            f"provider — not from your virtual key, which authenticated fine (the request "
            f"was routed and fallbacks were tried).\n\n"
            f"So the credential to fix is the one the PROXY holds for that model's "
            f"provider, not POCKETPAW_LITELLM_API_KEY.\n\n"
            f"To pick a model whose upstream is alive:\n"
            f'  curl -H "Authorization: Bearer $POCKETPAW_LITELLM_API_KEY" {base}/health\n'
            f"and set POCKETPAW_PYDANTIC_AI_MODEL=litellm:<a healthy model group>.\n\n"
            f"Original error: {text[:400]}"
        )

    def _map_event(self, event: Any, announced: set[str]) -> list[AgentEvent]:
        """Translate one pydantic-ai stream event into zero or more ``AgentEvent``.

        The mapping was read off the real event stream rather than the docs:

          PartStartEvent(TextPart)         -> message   (initial content, if any)
          PartDeltaEvent(TextPartDelta)    -> message
          PartStartEvent(ThinkingPart)     -> thinking
          PartDeltaEvent(ThinkingPartDelta)-> thinking
          PartStartEvent(ToolCallPart)     -> tool_use  (early UI signal)
          FunctionToolCallEvent            -> tool_use  (authoritative args)
          FunctionToolResultEvent          -> tool_result
          AgentRunResultEvent              -> token_usage
        """
        from pydantic_ai.messages import (
            FunctionToolCallEvent,
            FunctionToolResultEvent,
            PartDeltaEvent,
            PartStartEvent,
            TextPart,
            TextPartDelta,
            ThinkingPart,
            ThinkingPartDelta,
            ToolCallPart,
        )
        from pydantic_ai.run import AgentRunResultEvent

        out: list[AgentEvent] = []

        if isinstance(event, PartStartEvent):
            part = event.part
            if isinstance(part, TextPart) and part.content:
                out.append(AgentEvent(type="message", content=part.content))
            elif isinstance(part, ThinkingPart) and part.content:
                out.append(AgentEvent(type="thinking", content=part.content))
            elif isinstance(part, ToolCallPart):
                # Early signal so the UI flips from "Thinking..." to
                # "Using <tool>..." before the args finish streaming.
                self._announce_tool(part, announced, out, args={})

        elif isinstance(event, PartDeltaEvent):
            delta = event.delta
            if isinstance(delta, TextPartDelta) and delta.content_delta:
                out.append(AgentEvent(type="message", content=delta.content_delta))
            elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                out.append(AgentEvent(type="thinking", content=delta.content_delta))

        elif isinstance(event, FunctionToolCallEvent):
            self._announce_tool(event.part, announced, out, args=event.part.args)

        elif isinstance(event, FunctionToolResultEvent):
            part = event.part
            content = getattr(part, "content", "")
            text = content if isinstance(content, str) else str(content)
            out.append(
                AgentEvent(
                    type="tool_result",
                    content=text[:200],
                    metadata={"name": getattr(part, "tool_name", "tool")},
                )
            )

        elif isinstance(event, AgentRunResultEvent):
            usage_event = self._usage_event(event)
            if usage_event is not None:
                out.append(usage_event)

        return out

    @staticmethod
    def _announce_tool(part: Any, announced: set[str], out: list[AgentEvent], *, args: Any) -> None:
        """Emit a ``tool_use`` for *part* unless its call id was already announced."""
        name = getattr(part, "tool_name", None)
        if not name:
            return
        call_id = getattr(part, "tool_call_id", None)
        if call_id:
            if call_id in announced:
                return
            announced.add(call_id)
        out.append(
            AgentEvent(
                type="tool_use",
                content=f"Using {name}...",
                metadata={"name": name, "input": args if isinstance(args, dict) else {}},
            )
        )

    def _usage_event(self, event: Any) -> AgentEvent | None:
        """Build the ``token_usage`` event from a finished run's ``RunUsage``.

        ``RunUsage.input_tokens`` is the INCLUSIVE total — pydantic-ai documents
        cache reads/writes as subsets of it and normalizes the providers that
        report them disjointly. ``report_savings`` wants the Anthropic-native
        shape, where ``input_tokens`` is the UNCACHED remainder, so the two
        subsets come back out here. Getting this subtraction backwards inflates
        the reported hit rate, which is exactly the number the A/B turns on.
        """
        usage = getattr(getattr(event, "result", None), "usage", None)
        if usage is None:
            return None
        total = int(getattr(usage, "input_tokens", 0) or 0)
        read = int(getattr(usage, "cache_read_tokens", 0) or 0)
        write = int(getattr(usage, "cache_write_tokens", 0) or 0)
        if not read and not write:
            return None

        from pocketpaw.llm.caching import report_savings

        savings = report_savings(
            {
                "input_tokens": max(0, total - read - write),
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
            }
        )
        logger.info(
            "[pydantic_ai] prompt-cache: read=%d write=%d hit_rate=%.1f%% "
            "est_saved=%.0f input-tok-equiv",
            savings.cache_read_tokens,
            savings.cache_write_tokens,
            savings.hit_rate * 100,
            savings.est_tokens_saved,
        )
        return AgentEvent(
            type="token_usage",
            content="",
            metadata={
                "input_tokens": max(0, total - read - write),
                "cache_read_tokens": savings.cache_read_tokens,
                "cache_write_tokens": savings.cache_write_tokens,
                "cache_hit_rate": savings.hit_rate,
                "cache_est_tokens_saved": savings.est_tokens_saved,
                "backend": "pydantic_ai",
            },
        )

    # -- lifecycle ----------------------------------------------------------

    async def stop(self) -> None:
        """Signal every run live RIGHT NOW, then release MCP resources.

        Snapshot-then-signal, and no instance-level flag: a run started after
        this call gets a fresh ``_RunHandle`` and is unaffected. To cancel ONE
        run, close its generator (or cancel its task) instead — that is the
        per-run path, and it is what the cloud executor uses on supersession.
        """
        for handle in list(self._active):
            handle.stopped = True

        # Release the MCP servers this instance has been holding open. This is
        # the ONLY place they are torn down — the whole point of the exit stack
        # is that no per-run exit can do it.
        if self._mcp_stack is not None:
            stack, self._mcp_stack = self._mcp_stack, None
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP server shutdown error: %s", exc)
            finally:
                self._mcp_tools = None

    async def get_status(self) -> dict[str, Any]:
        provider, model = self._parse_provider_model()
        return {
            "backend": "pydantic_ai",
            "available": self._sdk_available,
            "running": bool(self._active),
            "active_runs": len(self._active),
            "mcp_servers": len(self._mcp_tools or ()),
            "model": self.settings.pydantic_ai_model,
            "provider": provider,
            "resolved_model": model,
        }
