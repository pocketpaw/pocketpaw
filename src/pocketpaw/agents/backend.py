"""Backend Protocol — the adapter interface all SDK backends implement.

Every agent backend (Claude SDK, OpenAI Agents, Gemini CLI, OpenCode CLI)
must expose a ``info()`` staticmethod and an async ``run()`` generator.
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
