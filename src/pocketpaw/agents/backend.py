"""Backend Protocol — the adapter interface all SDK backends implement.

Every agent backend (Claude SDK, OpenAI Agents, Gemini CLI, OpenCode CLI)
must expose a ``info()`` staticmethod and an async ``run()`` generator.

Updated: 2026-06-05 (feat/sites-svelte-engine) — the shared ``run`` signature
grows a ``deny_mcp_tool_ids: frozenset[str] = frozenset()`` keyword: a
per-surface MCP-tool deny set the chat loop threads through (resolved from the
request's ``SurfaceProfile``). Only the Claude SDK backend acts on it today
(subtracting the ids from its tool allowlist before launch); ``AgentPool.run``
only forwards it when non-empty, so backends that keep the narrower signature
are unaffected. It replaces the prompt-sniffing ripple-tool gate that lived in
``claude_sdk.py``.

Updated: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A1/A2) — the
shared ``run`` signature documents two more optional per-entity kwargs that ride
the same withhold-when-empty contract (``AgentPool.run`` forwards them only when
non-empty, so backends keeping the narrower signature are unaffected):
``allow_sdk_tools: frozenset[str]`` (additive SDK-tool allowlist, already
consumed by the Claude SDK backend) and ``skill_names: frozenset[str]`` (the
per-entity skill subset the Claude SDK backend materializes into a per-run local
plugin). The ``system_message_override`` field is applied UPSTREAM in
``AgentPool.run`` (it swaps the base system prompt before assembly), so it never
reaches a backend as a kwarg — it rides the existing ``system_prompt`` channel.

Updated: 2026-06-30 (feat/session-supervisor SS-1) — adds the ``SessionHandle``
dataclass: native-resume identity for a single agent session. It rides the SAME
withhold-when-empty contract as the kwargs above — ``AgentPool.run`` forwards a
``session_handle`` to ``backend.run`` ONLY when it is non-None, so the backends
that keep the narrower signature are unaffected, and only the Claude SDK backend
acts on it today (passing its ``cli_session_id`` as ``ClaudeAgentOptions.resume``
so a fresh-process turn resumes the on-disk conversation natively instead of
replaying Mongo history into the prompt). ``None`` is the unchanged legacy path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pocketpaw.config import Settings
    from pocketpaw.tools.policy import ToolPolicy

from pocketpaw.agents.protocol import AgentEvent  # re-export for convenience

# Default identity fallback shared across all backends.
# Used when AgentContextBuilder cannot supply a system prompt (e.g. empty
# identity files, first-run with no config, or legacy backend aliases).
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant running locally on the user's computer."
)


class Capability(Flag):
    """Feature flags advertised by a backend."""

    STREAMING = auto()
    TOOLS = auto()
    MCP = auto()
    MULTI_TURN = auto()
    CUSTOM_SYSTEM_PROMPT = auto()


@dataclass(frozen=True)
class BackendInfo:
    """Static metadata about a backend (no instance needed)."""

    name: str  # e.g. "claude_agent_sdk"
    display_name: str  # e.g. "Claude Agent SDK"
    capabilities: Capability
    builtin_tools: list[str] = field(default_factory=list)
    tool_policy_map: dict[str, str] = field(default_factory=dict)
    required_keys: list[str] = field(default_factory=list)
    supported_providers: list[str] = field(default_factory=list)
    install_hint: dict[str, str] = field(default_factory=dict)
    beta: bool = False


@dataclass
class SessionHandle:
    """SS-1 — native-resume identity for a single agent session.

    Carries the bookkeeping that lets ONE agent hold a conversation across
    turns via the Claude Agent SDK's NATIVE ``resume`` instead of replaying
    Mongo history into the prompt:

    * ``cli_session_id`` — the SDK session id captured on turn 1 (extracted
      from the init/system message the SDK emits at the start of a run). When
      set, the Claude SDK backend passes it as ``ClaudeAgentOptions.resume`` so
      a fresh-process turn resumes the on-disk conversation natively. ``None``
      (turn 1, or any non-supervised run) is the LEGACY path — behavior is
      unchanged from today.
    * ``session_store`` — carried through OPAQUELY for SS-2 (a custom SDK
      ``SessionStore``). SS-1 does NOT implement or consume it; it is an inert
      pass-through field only.

    Like ``deny_mcp_tool_ids`` / ``allow_sdk_tools`` / ``skill_names``, the
    handle rides the withhold-when-empty contract: ``AgentPool.run`` forwards it
    to ``backend.run`` ONLY when it is non-None, so backends that keep the
    narrower signature are unaffected. Only the Claude SDK backend acts on it.
    """

    cli_session_id: str | None = None
    session_store: Any | None = None


@runtime_checkable
class AgentBackend(Protocol):
    """Protocol that all agent backends must implement."""

    @staticmethod
    def info() -> BackendInfo: ...

    def __init__(self, settings: Settings) -> None: ...

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        skill_names: frozenset[str] = frozenset(),
    ) -> AsyncIterator[AgentEvent]: ...

    async def stop(self) -> None: ...

    async def get_status(self) -> dict[str, Any]: ...

    def get_tool_policy(self) -> ToolPolicy: ...

    def set_tool_policy(self, policy: ToolPolicy) -> None: ...

    def attach_specialist_tools(self, tools: list[Any]) -> None:
        """Attach pocket-specialist-internal tools to this backend instance.

        Called by the specialist runtime to wire list_pockets / validate_spec /
        persist_pocket into the LLM's tool surface for the duration of an
        isolated specialist run.

        Backends that cannot accept dynamic tools at runtime should raise
        NotImplementedError and will be excluded from the valid
        ``pocket_specialist_backend`` set.
        """
        ...

    def attach_subprocess_env(self, env: dict[str, str]) -> None:
        """Inject extra env vars into any subprocess this backend spawns.

        Used by the pocket-specialist runtime to thread per-request
        tenancy (``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        ``POCKETPAW_INTERNAL_TOKEN``) into the Claude Code subprocess
        WITHOUT mutating the parent process's ``os.environ`` (which
        would race across concurrent requests — see PR #1222 R1
        Blocker 1).

        Backends that don't spawn subprocesses can no-op safely.
        Backends that DO spawn one (claude_sdk, codex_cli) merge the
        dict into the env passed to that subprocess at spawn time.
        """
        ...


class BaseAgentBackend:
    """Default no-op implementations of optional ``AgentBackend`` methods.

    Backends that don't support a particular optional capability inherit
    from this mixin to get an informative ``NotImplementedError`` instead
    of an unhelpful ``AttributeError`` when callers try to use that
    capability.
    """

    def attach_specialist_tools(self, tools: list[Any]) -> None:  # noqa: ARG002
        raise NotImplementedError(
            f"{type(self).__name__} does not support dynamic tool attachment. "
            "Set POCKETPAW_POCKET_SPECIALIST_BACKEND=deep_agents (the default) "
            "to use a backend that supports specialist tool injection."
        )

    def attach_subprocess_env(self, env: dict[str, str]) -> None:  # noqa: ARG002
        """No-op default — backends that don't spawn subprocesses ignore.

        ``ClaudeSDKBackend`` overrides this to merge ``env`` into the
        Claude Code subprocess's ``options_kwargs["env"]``. The runtime
        calls this once per isolated specialist run to ship per-request
        tenancy values that the subprocess needs in its environment
        without polluting the parent's ``os.environ``.
        """
        return None
