"""LangChain React agent backend — thin alternative to ``deep_agents``.

Same model + tool + streaming surface as ``DeepAgentsBackend``, but the
compiled graph comes from ``langgraph.prebuilt.create_react_agent``
directly. Drops the ``deepagents`` middleware stack
(filesystem / subagents / summarization / todo) — pocket flow uses none
of it, and the summarization middleware can fire an extra LLM call
mid-stream which adds unpredictable latency.

Subclasses ``DeepAgentsBackend`` so the model build, MCP tool wiring,
custom tool bridge, streaming-event shape, and stop semantics stay
identical. Only the agent factory and the install check change.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.deep_agents import (
    DeepAgentsBackend,
    _patch_litellm_message_serializer,
    _patch_openai_message_serializer,
)

logger = logging.getLogger(__name__)


class LangchainReactBackend(DeepAgentsBackend):
    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="langchain_react",
            display_name="LangChain React (thin)",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=[],
            tool_policy_map={},
            required_keys=[],
            supported_providers=[
                "anthropic",
                "openai",
                "google",
                "ollama",
                "openrouter",
                "openai_compatible",
                "litellm",
            ],
            install_hint={
                "pip_package": "langgraph",
                "pip_spec": "pocketpaw[deep-agents]",
                "verify_import": "langgraph.prebuilt",
            },
            beta=True,
        )

    def _initialize(self) -> None:
        # Bypass parent's ``import deepagents`` check — we deliberately
        # don't depend on it.
        try:
            import langgraph.prebuilt  # noqa: F401

            self._sdk_available = True
            logger.info("LangChain React backend ready")
        except ImportError:
            self._sdk_available = False
            logger.warning("langchain_react backend requires langgraph (`pip install langgraph`).")

    def _get_or_create_agent(
        self, model: Any, instructions: str, mcp_tools: list | None = None
    ) -> Any:
        from langgraph.prebuilt import create_react_agent

        model_key = (self.settings.deep_agents_model,)
        if self._cached_agent is not None and self._cached_model_key == model_key:
            return self._cached_agent

        all_tools = self._build_custom_tools() + (mcp_tools or [])

        # DeepSeek thinking mode requires reasoning_content to be echoed
        # back on multi-turn requests with tool calls. Vanilla
        # langchain_openai / langchain_litellm don't preserve the field.
        # Apply the provider-specific message-serializer patch BEFORE the
        # agent is created so the round-trip works from turn 1.
        provider, _ = self._parse_provider_model()
        if provider == "litellm":
            _patch_litellm_message_serializer()
        elif provider in ("openai", "openai_compatible", "openrouter"):
            _patch_openai_message_serializer()

        agent = create_react_agent(
            model=model,
            tools=all_tools if all_tools else [],
            prompt=instructions,
        )
        self._cached_agent = agent
        self._cached_model_key = model_key
        return agent

    async def get_status(self) -> dict[str, Any]:
        status = await super().get_status()
        status["backend"] = "langchain_react"
        return status
