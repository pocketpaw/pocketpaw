"""Google Antigravity backend for PocketPaw.

Created: 2026-06-06 (feat/antigravity-backend) — adds the ``antigravity``
agent backend wrapping the official Google Antigravity Python SDK
(``pip install google-antigravity``), which provides:
- ``Agent`` + ``LocalAgentConfig`` — a batteries-included async agent that
  handles binary discovery, tool wiring, hook registration, and policy defaults
- Gemini-model reasoning with streamed text tokens (``async for token in resp``)
- Custom Python-callable tools (plain functions with docstrings)
- MCP stdio servers via ``McpStdioServer``

Requires: ``pip install 'pocketpaw[antigravity]'`` and a ``GEMINI_API_KEY``
(PocketPaw resolves it from ``antigravity_api_key`` → ``gemini_api_key`` →
``google_api_key``).

The SDK exposes thoughts/tool-calls as *separate* async iterators on the
response object, which makes true interleaved streaming awkward; v1 streams the
assistant text and emits a terminal ``done`` event. Tool-call surfacing in the
Activity panel is a tracked follow-up.

Updated 2026-06-08: wired ``antigravity_max_turns``. The SDK has no native turn
cap (the agentic loop runs inside ``Agent.chat()``), so ``_build_hooks`` adds a
``pre_tool_call_decide`` hook that counts tool calls and denies once the budget
is spent; ``run`` resets the counter per query and surfaces a notice when the
cap halts the loop.
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import (
    _DEFAULT_IDENTITY,
    BackendInfo,
    BaseAgentBackend,
    Capability,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)


class AntigravityBackend(BaseAgentBackend):
    """Google Antigravity backend — async SDK for Gemini-powered agents."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="antigravity",
            display_name="Google Antigravity",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["view_file", "edit_file", "run_command"],
            tool_policy_map={
                "view_file": "filesystem",
                "edit_file": "filesystem",
                "run_command": "shell",
            },
            # google_api_key is the field the desktop client's "google" provider
            # writes to (and the one google_adk advertises), so the same Gemini
            # key is reused across both backends. _resolve_api_key still prefers a
            # dedicated antigravity_api_key, then gemini_api_key, then this.
            required_keys=["google_api_key"],
            supported_providers=["google"],
            install_hint={
                "pip_package": "google-antigravity",
                "pip_spec": "pocketpaw[antigravity]",
                "verify_import": "google.antigravity",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._custom_tools: list | None = None
        # Per-run turn-limit bookkeeping, reset at the top of every run().
        self._turn_state: dict[str, Any] = {"count": 0, "limit_hit": False}
        # session_key -> accumulated history text (the SDK ``Agent`` is created
        # per-run, so multi-turn context is threaded via system instructions).
        self._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        self._initialize()

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy
        self._custom_tools = None

    # ------------------------------------------------------------------ init

    def _resolve_api_key(self) -> str | None:
        """Antigravity auths via ``GEMINI_API_KEY``.

        Resolve from the dedicated ``antigravity_api_key`` first, then fall back
        to the shared Gemini / Google keys so users who already configured
        Google ADK don't have to re-enter a key.
        """
        return (
            self.settings.antigravity_api_key
            or self.settings.gemini_api_key
            or self.settings.google_api_key
        )

    def _initialize(self) -> None:
        try:
            import google.antigravity  # noqa: F401

            self._sdk_available = True
            logger.info("Google Antigravity SDK ready")
        except ImportError:
            logger.warning(
                "Google Antigravity not installed -- pip install 'pocketpaw[antigravity]'"
            )
            return

        api_key = self._resolve_api_key()
        if api_key:
            # The SDK reads GEMINI_API_KEY from the environment; set it so the
            # plain ``LocalAgentConfig()`` path authenticates without an explicit
            # ``api_key=`` kwarg on SDK versions that don't accept one.
            os.environ.setdefault("GEMINI_API_KEY", api_key)

    # ------------------------------------------------------------------ tools

    def _build_custom_tools(self) -> list:
        """Lazily build PocketPaw custom tools as plain Antigravity callables.

        Antigravity accepts plain Python functions with docstrings as tools, so
        we reuse the tool_bridge wrapper builder. Cached for the instance — the
        tool set is fixed for a given policy.
        """
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_antigravity_function_tools

            self._custom_tools = build_antigravity_function_tools(
                self.settings, backend="antigravity", policy=self._policy
            )
        except Exception as exc:
            logger.debug("Could not build Antigravity custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    def _build_mcp_servers(self) -> list:
        """Build Antigravity ``McpStdioServer`` instances from PocketPaw MCP config."""
        try:
            from google.antigravity.types import McpStdioServer
        except ImportError:
            logger.debug("Antigravity MCP types not available")
            return []

        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            return []

        configs = load_mcp_config()
        if not configs:
            return []

        servers: list = []
        for cfg in configs:
            if cfg.transport != "stdio":
                # Antigravity's local agent wires stdio MCP servers; SSE/HTTP
                # servers are skipped (parity with the conservative ADK path).
                continue
            if not self._policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue
            try:
                # NOTE: McpStdioServer takes no ``env`` kwarg — environment for
                # the spawned server is inherited from the PocketPaw process.
                servers.append(
                    McpStdioServer(
                        name=cfg.name,
                        command=cfg.command,
                        args=cfg.args or [],
                    )
                )
            except Exception as exc:
                logger.debug("Skipping MCP server %s: %s", cfg.name, exc)

        logger.info("Built %d Antigravity MCP servers", len(servers))
        return servers

    def _build_hooks(self) -> list:
        """Build SDK hooks — currently just the per-query turn cap.

        ``antigravity_max_turns`` bounds how far the agentic loop may run
        (0 = unlimited). The SDK has no native turn knob: the loop lives inside
        ``Agent.chat()`` and its only in-loop interception point is the
        ``pre_tool_call_decide`` hook. So we count tool calls — each one drives
        another model step — and deny once the budget is spent. A denied call
        returns a message to the model, which then wraps up with a final answer
        instead of looping further. The decide hook is ANDed with the config's
        default policy (``confirm_run_command``): allowing a call here defers to
        the policy; denying short-circuits it.

        The counter lives on ``self._turn_state`` (reset per run) so ``run()``
        can surface a notice when the cap is hit.
        """
        max_turns = self.settings.antigravity_max_turns
        if not max_turns or max_turns <= 0:
            return []
        try:
            from google.antigravity.hooks import pre_tool_call_decide
            from google.antigravity.types import HookResult
        except ImportError:
            logger.debug("Antigravity hooks unavailable; turn limit not enforced")
            return []

        state = self._turn_state

        @pre_tool_call_decide
        async def _enforce_turn_limit(_tool_call: Any) -> Any:
            state["count"] += 1
            if state["count"] > max_turns:
                state["limit_hit"] = True
                return HookResult(
                    allow=False,
                    message=f"Max turns ({max_turns}) reached — stopping.",
                )
            return HookResult(allow=True)

        return [_enforce_turn_limit]

    def _build_config(self, instruction: str) -> Any:
        """Construct ``LocalAgentConfig`` defensively.

        The SDK's config signature varies across versions, so only forward
        kwargs the installed ``LocalAgentConfig`` actually accepts. Keeps the
        backend resilient to minor SDK drift instead of hard-failing on an
        unexpected keyword.
        """
        from google.antigravity import LocalAgentConfig

        candidate: dict[str, Any] = {
            "system_instructions": instruction,
            "tools": self._build_custom_tools(),
            "mcp_servers": self._build_mcp_servers(),
        }
        hooks = self._build_hooks()
        if hooks:
            candidate["hooks"] = hooks
        api_key = self._resolve_api_key()
        if api_key:
            candidate["api_key"] = api_key

        model = self.settings.antigravity_model
        if model:
            candidate["model"] = model

        try:
            accepted = set(inspect.signature(LocalAgentConfig).parameters)
        except (TypeError, ValueError):
            accepted = set(candidate)
        kwargs = {k: v for k, v in candidate.items() if k in accepted}
        return LocalAgentConfig(**kwargs)

    @staticmethod
    def _inject_history(instruction: str, history: list[dict]) -> str:
        """Append recent conversation to the system instruction as text."""
        lines = ["# Recent Conversation"]
        for msg in history:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"**{role}**: {content}")
        return instruction + "\n\n" + "\n".join(lines)

    # ------------------------------------------------------------------- run

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Google Antigravity not installed.\n\n"
                    "Install with: pip install 'pocketpaw[antigravity]'"
                ),
            )
            return

        if not self._resolve_api_key():
            yield AgentEvent(
                type="error",
                content=(
                    "No Gemini API key configured for the Antigravity backend.\n\n"
                    "Add one in **Settings > API Keys > Gemini API Key** "
                    "(get a key at https://aistudio.google.com/apikey)."
                ),
            )
            return

        self._stop_flag = False
        self._turn_state = {"count": 0, "limit_hit": False}

        try:
            from google.antigravity import Agent

            instruction = system_prompt or _DEFAULT_IDENTITY
            if history:
                instruction = self._inject_history(instruction, history)

            config = self._build_config(instruction)

            async with Agent(config) as agent:
                response = await agent.chat(message)

                async for token in response:
                    if self._stop_flag:
                        break
                    text = token if isinstance(token, str) else getattr(token, "text", "")
                    if text:
                        yield AgentEvent(type="message", content=text)

            # Token usage — the SDK exposes Gemini-style counts on the response.
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                yield AgentEvent(
                    type="token_usage",
                    content="",
                    metadata={
                        "input_tokens": getattr(usage, "prompt_token_count", 0) or 0,
                        "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
                        "model": self.settings.antigravity_model,
                        "backend": "antigravity",
                    },
                )

            # Surface a notice if the per-query turn cap halted the agentic loop
            # (parity with the Google ADK backend's max-turns handling).
            if self._turn_state.get("limit_hit"):
                yield AgentEvent(
                    type="error",
                    content=(
                        f"Max turns ({self.settings.antigravity_max_turns}) reached — "
                        "the response may be incomplete."
                    ),
                )

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("Google Antigravity error: %s", e)
            yield AgentEvent(type="error", content=f"Google Antigravity error: {e}")

    async def stop(self) -> None:
        self._stop_flag = True

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "antigravity",
            "available": self._sdk_available,
            "running": not self._stop_flag,
            "model": self.settings.antigravity_model,
        }
