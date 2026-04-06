"""LangChain Deep Agents backend for PocketPaw.

Uses the Deep Agents SDK (pip install deepagents) which provides:
- create_deep_agent() with built-in planning, filesystem, and subagent tools
- LangGraph runtime with durable execution and streaming
- Multi-provider LLM support via langchain init_chat_model
- Pluggable virtual filesystem backends

Requires: pip install deepagents
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import _DEFAULT_IDENTITY, BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)

# Maps PocketPaw provider names to LangChain init_chat_model provider names.
# Providers not listed here use the PocketPaw name as-is.
_LANGCHAIN_PROVIDER_MAP: dict[str, str] = {
    "google": "google_genai",
    "gemini": "google_genai",
    "openai_compatible": "openai",
    "openrouter": "openai",
}


def _unwrap(value: Any) -> Any:
    """Unwrap LangGraph Overwrite/Send wrapper objects to their inner value.

    LangGraph uses Overwrite() to signal state replacement in streaming updates.
    These objects are not iterable, so we need to extract the underlying value.
    """
    # Overwrite has a .value attribute containing the actual data
    if hasattr(value, "value"):
        return value.value
    return value


def _extract_content_text(content: Any) -> str:
    """Extract text from AIMessageChunk content.

    Content may be a plain string OR a list of content blocks
    (e.g. Anthropic returns [{"type": "text", "text": "..."}]).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


class DeepAgentsBackend:
    """Deep Agents backend -- LangChain/LangGraph agent framework."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="deep_agents",
            display_name="Deep Agents (LangChain)",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["write_todos", "read_todos", "task", "ls", "read_file", "write_file"],
            tool_policy_map={
                "write_file": "write_file",
                "read_file": "read_file",
                "task": "shell",
                "ls": "read_file",
            },
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
                "pip_package": "deepagents",
                "pip_spec": "pocketpaw[deep-agents]",
                "verify_import": "deepagents",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._custom_tools: list | None = None
        self._mcp_tools: list | None = None
        self._mcp_client: Any = None
        self._cached_agent: Any = None
        self._cached_model_key: str = ""
        self._initialize()

    def _initialize(self) -> None:
        try:
            import deepagents  # noqa: F401

            self._sdk_available = True
            logger.info("Deep Agents SDK ready")
        except ImportError:
            logger.warning("Deep Agents SDK not installed -- pip install 'pocketpaw[deep-agents]'")

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw tools as LangChain StructuredTool wrappers."""
        if self._custom_tools is not None:
            return self._custom_tools
        try:
            from pocketpaw.agents.tool_bridge import build_deep_agents_tools

            self._custom_tools = build_deep_agents_tools(self.settings, backend="deep_agents")
        except Exception as exc:
            logger.info("Could not build custom tools: %s", exc)
            self._custom_tools = []
        return self._custom_tools

    async def _build_mcp_tools(self) -> list:
        """Build LangChain tools from PocketPaw's configured MCP servers.

        Uses langchain-mcp-adapters to wrap MCP servers as LangChain tools
        that can be passed to create_deep_agent(). Requires the
        ``langchain-mcp-adapters`` package.
        """
        if self._mcp_tools is not None:
            return self._mcp_tools

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            logger.debug("langchain-mcp-adapters not installed, skipping MCP tools")
            self._mcp_tools = []
            return self._mcp_tools

        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            self._mcp_tools = []
            return self._mcp_tools

        from pocketpaw.tools.policy import ToolPolicy

        configs = load_mcp_config()
        if not configs:
            self._mcp_tools = []
            return self._mcp_tools

        policy = ToolPolicy(
            profile=self.settings.tool_profile,
            allow=self.settings.tools_allow,
            deny=self.settings.tools_deny,
        )

        # Build MultiServerMCPClient config from PocketPaw MCP configs
        client_config: dict[str, dict] = {}
        for cfg in configs:
            if not cfg.enabled:
                continue
            if not policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue

            if cfg.transport == "stdio" and cfg.command:
                client_config[cfg.name] = {
                    "transport": "stdio",
                    "command": cfg.command,
                    "args": cfg.args or [],
                    "env": cfg.env or None,
                }
            elif cfg.transport in ("sse", "http", "streamable-http") and cfg.url:
                transport = "http" if cfg.transport == "streamable-http" else cfg.transport
                client_config[cfg.name] = {
                    "transport": transport,
                    "url": cfg.url,
                }

        if not client_config:
            self._mcp_tools = []
            return self._mcp_tools

        try:
            self._mcp_client = MultiServerMCPClient(client_config)
            self._mcp_tools = await self._mcp_client.get_tools()
            logger.info("Built %d MCP tools for Deep Agents", len(self._mcp_tools))
        except Exception as exc:
            logger.warning("Failed to load MCP tools: %s", exc)
            self._mcp_tools = []

        return self._mcp_tools

    def _parse_provider_model(self) -> tuple[str, str]:
        """Parse provider and model from the deep_agents_model setting.

        Supports formats:
        - "provider:model" (e.g. "anthropic:claude-sonnet-4-6")
        - "model" alone (uses deep_agents_provider or falls back to "anthropic")
        """
        model_str = self.settings.deep_agents_model or ""
        if ":" in model_str:
            provider, _, model = model_str.partition(":")
            return provider.strip(), model.strip()
        # No provider prefix -- use the dedicated provider setting or fallback
        provider = getattr(self.settings, "deep_agents_provider", "auto")
        if provider == "auto":
            provider = self.settings.llm_provider
        if provider == "auto":
            provider = "anthropic"
        return provider, model_str.strip() or "claude-sonnet-4-6"

    def _build_model(self) -> Any:
        """Build the LangChain chat model with proper provider configuration.

        Resolves API keys, base URLs, and provider-specific settings from
        PocketPaw's config and passes them as kwargs to init_chat_model().
        """
        from langchain.chat_models import init_chat_model

        provider, model = self._parse_provider_model()
        kwargs: dict[str, Any] = {}

        if provider == "anthropic":
            if self.settings.anthropic_api_key:
                kwargs["api_key"] = self.settings.anthropic_api_key

        elif provider == "openai":
            if self.settings.openai_api_key:
                kwargs["api_key"] = self.settings.openai_api_key

        elif provider in ("google", "google_genai", "gemini"):
            provider = "google_genai"
            if self.settings.google_api_key:
                kwargs["google_api_key"] = self.settings.google_api_key

        elif provider == "ollama":
            host = self.settings.ollama_host or "http://localhost:11434"
            kwargs["base_url"] = host
            if not model:
                model = self.settings.ollama_model or "llama3.2"

        elif provider == "openrouter":
            kwargs["base_url"] = "https://openrouter.ai/api/v1"
            api_key = self.settings.openrouter_api_key or self.settings.openai_compatible_api_key
            if api_key:
                kwargs["api_key"] = api_key
            if not model:
                model = self.settings.openrouter_model or ""
            # OpenRouter uses OpenAI-compatible API
            provider = "openai"

        elif provider == "openai_compatible":
            if self.settings.openai_compatible_base_url:
                kwargs["base_url"] = self.settings.openai_compatible_base_url
            api_key = self.settings.openai_compatible_api_key
            if api_key:
                kwargs["api_key"] = api_key
            if not model:
                model = self.settings.openai_compatible_model or ""
            provider = "openai"

        elif provider == "litellm":
            # Route through LiteLLM proxy as OpenAI-compatible endpoint.
            # The proxy exposes an OpenAI API, so we use the openai provider
            # pointed at the proxy URL. This avoids needing langchain-litellm.
            base = (self.settings.litellm_api_base or "http://localhost:4000").rstrip("/")
            kwargs["base_url"] = f"{base}/v1"
            kwargs["api_key"] = self.settings.litellm_api_key or "not-needed"
            if not model:
                model = self.settings.litellm_model or ""
            provider = "openai"

        # Map to LangChain's expected provider name
        lc_provider = _LANGCHAIN_PROVIDER_MAP.get(provider, provider)
        model_id = f"{lc_provider}:{model}" if model else lc_provider

        logger.info("Deep Agents: init_chat_model(%r) with %d kwargs", model_id, len(kwargs))
        return init_chat_model(model_id, **kwargs)

    def _get_or_create_agent(
        self, model: Any, instructions: str, mcp_tools: list | None = None
    ) -> Any:
        """Cache the compiled LangGraph agent to avoid recompilation on every call."""
        from deepagents import create_deep_agent

        # Invalidate cache if model setting changed
        model_key = self.settings.deep_agents_model
        if self._cached_agent is not None and self._cached_model_key == model_key:
            return self._cached_agent

        all_tools = self._build_custom_tools() + (mcp_tools or [])
        agent = create_deep_agent(
            model=model,
            tools=all_tools if all_tools else [],
            system_prompt=instructions,
        )
        self._cached_agent = agent
        self._cached_model_key = model_key
        return agent

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
                    "Deep Agents SDK not installed.\n\n"
                    "Install with: pip install 'pocketpaw[deep-agents]'"
                ),
            )
            return

        self._stop_flag = False

        try:
            model = self._build_model()
            instructions = system_prompt or _DEFAULT_IDENTITY

            # Load MCP tools from configured servers (async, cached after first call)
            mcp_tools = await self._build_mcp_tools()
            agent = self._get_or_create_agent(model, instructions, mcp_tools=mcp_tools)

            # Build messages list: history + current message
            messages: list[dict[str, str]] = []
            if history:
                for msg in history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if content:
                        messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": message})

            # Set recursion_limit from max_turns setting
            config: dict[str, Any] = {}
            max_turns = self.settings.deep_agents_max_turns
            if max_turns and max_turns > 0:
                # LangGraph recursion_limit controls max graph steps.
                # Each tool round-trip is ~2-3 steps, so multiply for headroom.
                config["recursion_limit"] = max_turns * 3

            # Stream using LangGraph's async streaming
            async for chunk in agent.astream(
                {"messages": messages},
                stream_mode=["updates", "messages"],
                version="v2",
                config=config if config else None,
            ):
                if self._stop_flag:
                    break

                if not isinstance(chunk, dict):
                    continue
                chunk_type = chunk.get("type", "")

                if chunk_type == "messages":
                    data = chunk.get("data")
                    if data is None:
                        continue
                    # v2 format: data is (AIMessageChunk, metadata_dict) tuple
                    msg_chunk = data[0] if isinstance(data, tuple | list) else data
                    content = _extract_content_text(getattr(msg_chunk, "content", ""))
                    if content:
                        yield AgentEvent(type="message", content=content)

                elif chunk_type == "updates":
                    data = _unwrap(chunk.get("data", {}))
                    if not isinstance(data, dict):
                        continue
                    for _node_name, node_data in data.items():
                        node_data = _unwrap(node_data)
                        if not isinstance(node_data, dict):
                            continue
                        node_messages = _unwrap(node_data.get("messages", []))
                        if not isinstance(node_messages, list):
                            continue
                        for msg in node_messages:
                            # Tool call messages
                            tool_calls = getattr(msg, "tool_calls", None)
                            if tool_calls:
                                for tc in tool_calls:
                                    name = tc.get("name", "Tool")
                                    yield AgentEvent(
                                        type="tool_use",
                                        content=f"Using {name}...",
                                        metadata={
                                            "name": name,
                                            "input": tc.get("args", {}),
                                        },
                                    )
                            # Tool response messages
                            if getattr(msg, "type", "") == "tool":
                                tool_name = getattr(msg, "name", "tool")
                                tool_content = getattr(msg, "content", "")
                                if isinstance(tool_content, str):
                                    tool_content = tool_content[:200]
                                else:
                                    tool_content = str(tool_content)[:200]
                                yield AgentEvent(
                                    type="tool_result",
                                    content=tool_content,
                                    metadata={"name": tool_name},
                                )

            yield AgentEvent(type="done", content="")

        except Exception as e:
            logger.error("Deep Agents streaming error: %s", e, exc_info=True)
            yield AgentEvent(type="error", content=f"Deep Agents error: {e}")
            yield AgentEvent(type="done", content="")

    async def stop(self) -> None:
        self._stop_flag = True
        # Clean up MCP client resources if they were allocated
        if self._mcp_client is not None:
            try:
                close = getattr(self._mcp_client, "close", None) or getattr(
                    self._mcp_client, "aclose", None
                )
                if close:
                    await close()
            except Exception as exc:
                logger.debug("MCP client cleanup error: %s", exc)
            finally:
                self._mcp_client = None
                self._mcp_tools = None

    async def get_status(self) -> dict[str, Any]:
        provider, model = self._parse_provider_model()
        return {
            "backend": "deep_agents",
            "available": self._sdk_available,
            "running": not self._stop_flag,
            "model": self.settings.deep_agents_model,
            "provider": provider,
            "resolved_model": model,
        }
