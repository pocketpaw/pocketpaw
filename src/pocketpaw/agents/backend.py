"""Backend Protocol — the adapter interface all SDK backends implement.

Every agent backend (Claude SDK, OpenAI Agents, Gemini CLI, OpenCode CLI)
must expose a ``info()`` staticmethod and an async ``run()`` generator.

Updated: 2026-06-30 (feat/warm-reuse WH-1) — adds the ``LeasedClient`` dataclass:
a connected, CALLER-owned warm ``ClaudeSDKClient`` plus the backend cache key it
was connected under. The ``SessionSupervisor`` (WH-2/WH-3) owns a per-(workspace,
session) live client and LEASES it into ``ClaudeSDKBackend.run`` so turn 2+ reuses
the live subprocess instead of resuming COLD. Generic by design (``client: Any``)
so OSS / the supervisor hold it without importing the concrete SDK type — the same
opaque pass-through discipline as ``SessionHandle.session_store`` — and it lives
here (not in ``claude_sdk``) so the supervisor can import it without an import
cycle. The backend never owns or tears down a leased client; the supervisor keeps
it warm across turns and disconnects it on its own lifecycle.

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

Updated: 2026-06-30 (feat/session-supervisor SS-2) — the ``SessionHandle.session_store``
field is now real: a tenancy-keyed custom SDK ``SessionStore``. The Claude SDK
backend forwards it OPAQUELY to ``ClaudeAgentOptions.session_store`` so a resume
turn reconstructs the conversation from OUR durable store (not local disk),
namespaced by ``(workspace_id, project_key, session_id)`` so one tenant can never
read another's session. SS-1's ``cli_session_id`` resume wiring is unchanged.

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
    * ``session_store`` — a custom SDK ``SessionStore`` (SS-2). The Claude SDK
      backend forwards it OPAQUELY to ``ClaudeAgentOptions.session_store`` when
      non-None, so a resume turn materializes the conversation from OUR store
      (tenancy-keyed by ``(workspace_id, project_key, session_id)`` — see
      ``pocketpaw.agents.session_store.InMemorySessionStore`` and the ee
      Mongo-backed ``MongoSessionStore``) instead of local disk. It rides
      through opaquely so OSS never imports the concrete (possibly ee) store
      class. ``None`` is the unchanged legacy path.

    Like ``deny_mcp_tool_ids`` / ``allow_sdk_tools`` / ``skill_names``, the
    handle rides the withhold-when-empty contract: ``AgentPool.run`` forwards it
    to ``backend.run`` ONLY when it is non-None, so backends that keep the
    narrower signature are unaffected. Only the Claude SDK backend acts on it.
    """

    cli_session_id: str | None = None
    session_store: Any | None = None


@dataclass
class LeasedClient:
    """WH-1 — a connected, caller-owned warm SDK client leased into a backend run.

    The ``SessionSupervisor`` (WH-2/WH-3) owns a per-(workspace, session) live
    ``ClaudeSDKClient`` and LEASES it to ``ClaudeSDKBackend.run`` so turn 2+ reuses
    the live subprocess instead of resuming COLD (re-materialize + a fresh
    ``claude`` connect). The backend NEVER owns or tears down a leased client — the
    supervisor keeps it warm across turns and disconnects it on its own lifecycle.

    * ``client`` — a connected ``ClaudeSDKClient``. Typed ``Any`` so OSS / the
      supervisor can hold it without importing the concrete SDK type, mirroring
      ``SessionHandle.session_store``'s opaque pass-through.
    * ``options_key`` — the backend's ``_client_cache_key`` for the options the
      client was connected with (session + cwd + model + tools + system-prompt
      behavioral-prefix digest + plugin-identity digest). The backend recomputes
      THIS turn's key and reuses the leased client ONLY on an exact match; a
      mismatch routes to a fresh build (and the supervisor rebinds the new slot).
    * ``busy`` — set by the backend while it is driving a query on ``client`` so a
      second concurrent turn never drives two queries on one subprocess. A busy
      lease makes the second turn fall back to a fresh stateless client for that
      turn (it does not block, corrupt the shared client, or rebind the slot).

    Rides the same withhold-when-empty contract as ``SessionHandle``: ``AgentPool.run``
    forwards ``warm_client`` to ``backend.run`` only when non-None, so backends that
    keep the narrower signature are unaffected. Only the Claude SDK backend acts on it.
    """

    client: Any
    options_key: str
    busy: bool = False


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
