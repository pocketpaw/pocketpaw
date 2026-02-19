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

from pocketpaw.agents.protocol import AgentEvent  # re-export for convenience


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
