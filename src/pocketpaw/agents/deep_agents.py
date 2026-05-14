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


_LITELLM_PATCHED = False
_OPENAI_PATCHED = False
_ANTHROPIC_PATCHED = False

# Threshold above which we tag the system block with ``cache_control``.
# Anthropic's prompt-cache minimum is ~1024 tokens on Sonnet/Opus and
# ~2048 on Haiku; one English token ≈ 4 chars, so 4000 chars is well
# clear of the Sonnet floor but still excludes the small lifestyle
# prompts the chat agent uses for greetings / one-shot facts. Tuned
# conservatively — false positives only cost the cache-write overhead.
_ANTHROPIC_CACHE_MIN_CHARS = 4000


def _patch_openai_message_serializer() -> None:
    """Make langchain_openai round-trip DeepSeek `reasoning_content` when
    the direct-DeepSeek (openai-compat) route is in use.

    DeepSeek thinking mode requires ``reasoning_content`` from each prior
    assistant turn to be echoed back on subsequent multi-turn requests
    (per https://api-docs.deepseek.com/guides/thinking_mode). Vanilla
    ``langchain_openai`` ignores the field on the way in AND drops
    ``additional_kwargs`` on the way out, so every tool-using agent
    400s on its second API call ("``reasoning_content`` in thinking
    mode must be passed back to the API").

    Three patches on ``langchain_openai.chat_models.base``:
      1. ``_convert_dict_to_message`` — non-streaming response: capture
         ``reasoning_content`` into ``AIMessage.additional_kwargs``.
      2. ``_convert_delta_to_message_chunk`` — streaming delta: same on
         ``AIMessageChunk``, accumulating across chunks.
      3. ``_convert_message_to_dict`` — outbound: re-emit
         ``reasoning_content`` as a top-level field when an assistant
         message carries it in ``additional_kwargs``.

    All three are idempotent and a no-op for non-DeepSeek openai-compat
    endpoints (they only act when the field is actually present), so
    applying unconditionally is safe.
    """
    global _OPENAI_PATCHED
    if _OPENAI_PATCHED:
        return

    try:
        from langchain_openai.chat_models import base as _oa
    except ImportError:
        return

    # The three target symbols are private (``_convert_*``) and may be
    # renamed or moved by a future langchain-openai release. The
    # try/except above catches a missing module; the ``hasattr`` guards
    # below catch a missing attribute, log loudly so a langchain upgrade
    # surfaces in CI logs (not as a silent AttributeError on the first
    # DeepSeek call in production), and skip the individual patch
    # without breaking the others.
    _patched_count = 0
    _missing: list[str] = []

    # --- 1. inbound: non-streaming response → AIMessage ---
    if hasattr(_oa, "_convert_dict_to_message"):
        original_dict_to_message = _oa._convert_dict_to_message

        def patched_dict_to_message(_dict):  # type: ignore[no-untyped-def]
            msg = original_dict_to_message(_dict)
            if msg.type == "ai":
                rc = _dict.get("reasoning_content")
                if rc:
                    msg.additional_kwargs["reasoning_content"] = rc
            return msg

        _oa._convert_dict_to_message = patched_dict_to_message
        _patched_count += 1
    else:
        _missing.append("_convert_dict_to_message")

    # --- 2. inbound: streaming delta → AIMessageChunk ---
    if hasattr(_oa, "_convert_delta_to_message_chunk"):
        original_delta_to_chunk = _oa._convert_delta_to_message_chunk

        def patched_delta_to_chunk(_dict, default_class):  # type: ignore[no-untyped-def]
            chunk = original_delta_to_chunk(_dict, default_class)
            rc = _dict.get("reasoning_content")
            if rc and hasattr(chunk, "additional_kwargs"):
                existing = chunk.additional_kwargs.get("reasoning_content") or ""
                chunk.additional_kwargs["reasoning_content"] = existing + rc
            return chunk

        _oa._convert_delta_to_message_chunk = patched_delta_to_chunk
        _patched_count += 1
    else:
        _missing.append("_convert_delta_to_message_chunk")

    # --- 3. outbound: AIMessage → request dict ---
    if hasattr(_oa, "_convert_message_to_dict"):
        original_message_to_dict = _oa._convert_message_to_dict

        def patched_message_to_dict(message, api="chat/completions"):  # type: ignore[no-untyped-def]
            msg_dict = original_message_to_dict(message, api=api)
            if msg_dict.get("role") == "assistant":
                rc = getattr(message, "additional_kwargs", {}).get("reasoning_content")
                if rc and not msg_dict.get("reasoning_content"):
                    msg_dict["reasoning_content"] = rc
            return msg_dict

        _oa._convert_message_to_dict = patched_message_to_dict
        _patched_count += 1
    else:
        _missing.append("_convert_message_to_dict")

    _OPENAI_PATCHED = True
    if _missing:
        logger.error(
            "langchain_openai upgrade broke the DeepSeek reasoning_content patch: "
            "missing %s on langchain_openai.chat_models.base. Multi-turn DeepSeek "
            "thinking-mode calls will 400. Update the patch in "
            "src/pocketpaw/agents/deep_agents.py:_patch_openai_message_serializer.",
            ", ".join(_missing),
        )
    if _patched_count:
        logger.info(
            "Patched %d/3 langchain_openai message serializers for DeepSeek "
            "reasoning_content round-trip",
            _patched_count,
        )


def _patch_litellm_message_serializer() -> None:
    """Make langchain_litellm round-trip DeepSeek `reasoning_content` properly.

    langchain_litellm wraps DeepSeek's `reasoning_content` as an Anthropic-style
    `{"type": "thinking", "thinking": "..."}` content block on the AIMessage,
    but its outbound `_convert_message_to_dict` passes that block through
    untouched and never re-emits a top-level `reasoning_content` field. DeepSeek
    rejects both shapes: the block is an unknown variant, and stripping it
    makes DeepSeek complain that reasoning_content is missing in thinking mode.

    Patch the serializer to:
      - filter `thinking` / `redacted_thinking` blocks out of `content`
      - hoist their text back into a top-level `reasoning_content` field
    """
    global _LITELLM_PATCHED
    if _LITELLM_PATCHED:
        return

    try:
        from langchain_litellm.chat_models import litellm as _ll
    except ImportError:
        return

    original = _ll._convert_message_to_dict

    def patched(message):  # type: ignore[no-untyped-def]
        msg_dict = original(message)
        content = msg_dict.get("content")
        if isinstance(content, list):
            thinking_parts: list[str] = []
            kept: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "thinking",
                    "redacted_thinking",
                ):
                    text = block.get("thinking") or block.get("data") or ""
                    if text:
                        thinking_parts.append(text)
                    continue
                kept.append(block)
            if thinking_parts:
                if not msg_dict.get("reasoning_content"):
                    msg_dict["reasoning_content"] = "\n".join(thinking_parts)
                msg_dict["content"] = kept or ""
        # Also surface reasoning_content stashed in additional_kwargs by the
        # response parser, in case content was already a plain string.
        if not msg_dict.get("reasoning_content"):
            rc = getattr(message, "additional_kwargs", {}).get("reasoning_content")
            if rc:
                msg_dict["reasoning_content"] = rc
        return msg_dict

    _ll._convert_message_to_dict = patched
    _LITELLM_PATCHED = True
    logger.info(
        "Patched langchain_litellm._convert_message_to_dict for reasoning_content round-trip"
    )


def _patch_anthropic_message_serializer() -> None:
    """Enable Anthropic prompt caching on long system messages.

    Anthropic's prompt cache is opt-in per content block: a block must
    carry ``cache_control: {"type": "ephemeral"}`` for the API to cache
    its tokenized prefix. LangChain's ``_format_messages`` passes a
    string-typed ``SystemMessage.content`` straight through to the API's
    ``system`` parameter — no markup, no caching. For the pocket
    specialist's ~12k-token design-rules prompt that means we re-pay
    the prefix tokenization on every spec generation.

    This patch wraps ``langchain_anthropic.chat_models._format_messages``:
    after the upstream conversion runs, we lift a long string-typed
    system value into a single-block list with ``cache_control``
    attached, and we annotate the last text block when the system is
    already a list (typical: the message left the SystemMessage as a
    structured content object). Short prompts (< ``_ANTHROPIC_CACHE_MIN_CHARS``)
    are left alone — cache overhead outweighs savings on small prompts.

    Idempotent: re-imports of this module reuse the same patched
    function via the ``_ANTHROPIC_PATCHED`` sentinel.
    """
    global _ANTHROPIC_PATCHED
    if _ANTHROPIC_PATCHED:
        return

    try:
        from langchain_anthropic import chat_models as _ac
    except ImportError:
        return

    if not hasattr(_ac, "_format_messages"):
        logger.error(
            "langchain_anthropic upgrade broke the prompt-cache patch: "
            "missing _format_messages on langchain_anthropic.chat_models. "
            "Anthropic prompt caching is disabled for this run. Update "
            "src/pocketpaw/agents/deep_agents.py:_patch_anthropic_message_serializer."
        )
        return

    original = _ac._format_messages

    def patched(messages):  # type: ignore[no-untyped-def]
        system, formatted = original(messages)
        if isinstance(system, str) and len(system) >= _ANTHROPIC_CACHE_MIN_CHARS:
            system = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        elif isinstance(system, list) and system:
            # Already a block list. Tag the last text block whose total
            # text size pushes the system payload over the threshold —
            # short text-only systems and pure-image systems are skipped.
            total_chars = sum(
                len(b.get("text", ""))
                for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            )
            already_cached = any(
                isinstance(b, dict) and b.get("cache_control") for b in system
            )
            if total_chars >= _ANTHROPIC_CACHE_MIN_CHARS and not already_cached:
                for block in reversed(system):
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["cache_control"] = {"type": "ephemeral"}
                        break
        return system, formatted

    _ac._format_messages = patched
    _ANTHROPIC_PATCHED = True
    logger.info(
        "Patched langchain_anthropic._format_messages for prompt caching "
        "(threshold=%d chars)",
        _ANTHROPIC_CACHE_MIN_CHARS,
    )


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


def _split_content_text_and_thinking(content: Any) -> tuple[str, str]:
    """Return ``(text, thinking)`` extracted from an AIMessageChunk
    content payload. Anthropic emits ``{"type":"thinking","thinking":...}``
    blocks; ``langchain_litellm`` wraps DeepSeek reasoning_content the
    same way. Both feed the SSE ``thinking`` event so the UI shows
    activity instead of silent dead time."""
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype in ("thinking", "redacted_thinking"):
            thinking_parts.append(block.get("thinking") or block.get("data") or "")
    return "".join(text_parts), "".join(thinking_parts)


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
        self._cached_model_key: Any = None
        self._initialize()

    def _initialize(self) -> None:
        try:
            import deepagents  # noqa: F401

            self._sdk_available = True
            logger.info("Deep Agents SDK ready")
        except ImportError:
            logger.warning("Deep Agents SDK not installed -- pip install 'pocketpaw[deep-agents]'")

    def _build_custom_tools(self) -> list:
        """Lazily build and cache PocketPaw tools as LangChain StructuredTool wrappers.

        Note: when this backend is used in an isolated specialist run, the
        runtime calls ``attach_specialist_tools`` first, which pre-populates
        ``_custom_tools`` with only the specialist's internal tools. This
        method then early-returns the cached list, so the
        ``pocket_specialist__create`` tool (auto-injected by the bridge for
        every main-agent run) is NOT pulled into the specialist's own
        backend, which would be a recursion footgun.
        """
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
        # OpenAI-compat endpoints that speak chat-completions but NOT the
        # OpenAI Responses API (DeepSeek, OpenRouter, LiteLLM proxy, vLLM,
        # etc.). When provider is mapped to "openai" with a custom base_url,
        # we must force chat-completions or init_chat_model defaults to the
        # Responses API in deepagents 0.5.x and the call fails with 404.
        is_openai_compat_endpoint = False

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
            is_openai_compat_endpoint = True

        elif provider == "openai_compatible":
            if self.settings.openai_compatible_base_url:
                kwargs["base_url"] = self.settings.openai_compatible_base_url
            api_key = self.settings.openai_compatible_api_key
            if api_key:
                kwargs["api_key"] = api_key
            if not model:
                model = self.settings.openai_compatible_model or ""
            provider = "openai"
            is_openai_compat_endpoint = True

        elif provider == "litellm":
            # Route through LiteLLM via the native ChatLiteLLM integration. The
            # LiteLLM SDK handles provider-specific quirks (DeepSeek
            # reasoning_content threading, Anthropic thinking blocks,
            # model-name routing) that our earlier ChatOpenAI-masquerade
            # dropped on the floor. ChatLiteLLM uses ``api_base`` (not
            # ``base_url``) and the LiteLLM SDK appends the path itself, so we
            # must NOT add ``/v1``. We also keep provider="litellm" so
            # init_chat_model returns ChatLiteLLM, and we leave
            # ``is_openai_compat_endpoint`` False — ChatLiteLLM does not
            # accept ``use_responses_api``.
            base = (self.settings.litellm_api_base or "http://localhost:4000").rstrip("/")
            kwargs["api_base"] = base
            kwargs["api_key"] = self.settings.litellm_api_key or "not-needed"
            if not model:
                model = self.settings.litellm_model or ""

        # Force chat-completions for non-OpenAI endpoints. The default
        # Responses API in deepagents 0.5.x is OpenAI-only.
        if is_openai_compat_endpoint:
            kwargs["use_responses_api"] = False

        # Providers diverge on where the disable-thinking flag goes:
        #
        # - litellm           — ChatLiteLLM drops unknown top-level
        #                       kwargs silently. extra_body MUST go
        #                       through model_kwargs.
        #                       NOTE: LiteLLM's DeepSeekChatConfig
        #                       strips thinking={"type":"disabled"}
        #                       before sending. Disable only actually
        #                       works if the proxy is in raw passthrough
        #                       mode or runs a patched LiteLLM. See
        #                       https://github.com/BerriAI/litellm/issues/27439
        # - openai / openai_compatible — ChatOpenAI has extra_body as a
        #                       native top-level field; OpenAI SDK
        #                       forwards it verbatim in the request body.
        #                       Hitting DeepSeek's API directly via this
        #                       path BYPASSES both LiteLLM transformers
        #                       (ours + any proxy) — the recommended
        #                       path for reliable disable.
        # - anthropic         — `thinking={"type":"disabled"}` is a
        #                       top-level kwarg the SDK honors directly.
        #
        # `reasoning_effort` is deliberately NOT passed. It's a thinking-
        # mode parameter; setting it alongside disable is contradictory.
        if getattr(self.settings, "deep_agents_disable_thinking", False):
            if provider == "litellm":
                model_kwargs = dict(kwargs.get("model_kwargs") or {})
                extra_body = dict(model_kwargs.get("extra_body") or {})
                extra_body["thinking"] = {"type": "disabled"}
                model_kwargs["extra_body"] = extra_body
                kwargs["model_kwargs"] = model_kwargs
            elif provider == "openai":
                # ChatOpenAI accepts extra_body as a native field — set
                # it top-level. Covers both `openai` and the
                # `openai_compatible` path which we remap to `openai`
                # above with a custom base_url.
                existing = kwargs.get("extra_body") or {}
                kwargs["extra_body"] = {
                    **existing,
                    "thinking": {"type": "disabled"},
                }
            else:
                kwargs["thinking"] = {"type": "disabled"}

        # Map to LangChain's expected provider name
        lc_provider = _LANGCHAIN_PROVIDER_MAP.get(provider, provider)
        model_id = f"{lc_provider}:{model}" if model else lc_provider

        # Surface the thinking-disable status loudly. Past bug: extra_body
        # was set at the wrong nesting level and silently dropped. This
        # log makes any future regression visible on first inspection.
        mk = kwargs.get("model_kwargs") or {}
        if isinstance(mk, dict) and "extra_body" in mk:
            logger.info(
                "Deep Agents: init_chat_model(%r) with model_kwargs.extra_body=%s",
                model_id,
                mk["extra_body"],
            )
        else:
            logger.info(
                "Deep Agents: init_chat_model(%r) with %d kwargs",
                model_id,
                len(kwargs),
            )
        return init_chat_model(model_id, **kwargs)

    def _get_or_create_agent(
        self, model: Any, instructions: str, mcp_tools: list | None = None
    ) -> Any:
        """Cache the compiled LangGraph agent to avoid recompilation on every call."""
        from deepagents import create_deep_agent

        skills = list(self.settings.deep_agents_skills or [])
        memory = list(self.settings.deep_agents_memory or [])

        # Pocket sessions don't need shell or filesystem access. Same gate
        # as claude_sdk: <pocket-scope> appears in every pocket prompt.
        is_pocket_session = "<pocket-scope>" in (instructions or "")

        # Invalidate cache if any input that shapes the compiled graph changed.
        # is_pocket_session is part of the key so flipping between pocket and
        # non-pocket sessions in the same backend recompiles the agent.
        model_key = (
            self.settings.deep_agents_model,
            tuple(skills),
            tuple(memory),
            is_pocket_session,
        )
        if self._cached_agent is not None and self._cached_model_key == model_key:
            return self._cached_agent

        all_tools = self._build_custom_tools() + (mcp_tools or [])
        if is_pocket_session:
            # Drop shell + filesystem tools — pocket flow has MCP tools
            # for everything it needs. Without this filter the agent has
            # been observed running `env | grep pocket; curl localhost`
            # to introspect state.
            _blocked = {
                "shell",
                "read_file",
                "write_file",
                "edit_file",
                "list_dir",
            }
            before = len(all_tools)
            all_tools = [t for t in all_tools if getattr(t, "name", "") not in _blocked]
            if before != len(all_tools):
                logger.info(
                    "Pocket session — stripped %d shell/fs tools from agent",
                    before - len(all_tools),
                )
        kwargs: dict[str, Any] = {
            "model": model,
            "tools": all_tools if all_tools else [],
            "system_prompt": instructions,
        }
        # Only forward skills/memory when populated — passing empty lists
        # still wires SkillsMiddleware/MemoryMiddleware with nothing to load.
        if skills:
            kwargs["skills"] = skills
        if memory:
            kwargs["memory"] = memory

        # Patch the active provider's message serializer so DeepSeek-thinking
        # `reasoning_content` round-trips correctly across multi-turn / tool-
        # using conversations. Both patches are idempotent and no-op when the
        # field is absent (non-DeepSeek endpoints), so apply unconditionally
        # for the provider that's actually in play.
        #
        # We KEEP the patch on regardless of `deep_agents_disable_thinking`.
        # That flag only suppresses Anthropic thinking — DeepSeek ignores it
        # and keeps emitting reasoning_content. If we skipped the patch when
        # the flag is set, DeepSeek-routed conversations would 400 on every
        # multi-turn ("reasoning_content in thinking mode must be passed
        # back to the API").
        provider, _ = self._parse_provider_model()
        if provider == "litellm":
            _patch_litellm_message_serializer()
        elif provider in ("openai", "openai_compatible", "openrouter"):
            # The openai_compatible / openrouter / direct-DeepSeek route uses
            # ChatOpenAI under the hood. Patch the langchain_openai
            # serializers — see _patch_openai_message_serializer docstring.
            _patch_openai_message_serializer()
        elif provider == "anthropic":
            # ChatAnthropic does not tag long system blocks with
            # cache_control by default, so the pocket specialist's
            # ~12k-token design-rules prompt re-tokenizes on every call.
            # The patch wraps _format_messages to add cache_control on
            # the longest system text block. See the patch docstring.
            _patch_anthropic_message_serializer()

        agent = create_deep_agent(**kwargs)
        self._cached_agent = agent
        self._cached_model_key = model_key
        return agent

    def attach_specialist_tools(self, tools: list[Any]) -> None:
        """Merge specialist tools into the custom-tool cache for an isolated run.

        Also short-circuits MCP-server loading by pre-setting ``_mcp_tools = []``.
        Specialist runs are short-lived and only need the tools passed here;
        loading the user's full MCP server set (which can include slow stdio
        servers) wastes startup time and risks hanging the run.

        Each call extends the list; tools are not deduplicated. Use an isolated
        backend instance (AgentRouter.create_isolated_backend) to avoid
        accumulation across specialist runs.
        """
        if self._custom_tools is None:
            self._custom_tools = []
        self._custom_tools.extend(tools)
        # Skip MCP loading — specialist's tool surface comes entirely from
        # `tools` above. _build_mcp_tools() short-circuits when _mcp_tools is
        # not None.
        self._mcp_tools = []
        self._cached_agent = None  # force recompile next run
        self._cached_model_key = None

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
        # Stream handle captured in the run-level scope so the ``finally``
        # block can close it on every exit path (normal completion,
        # except branch, generator-close from caller).
        _stream: Any = None

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

            # Track tool_use emissions so we don't double-announce: the
            # "messages" path emits as soon as a tool NAME appears in a
            # tool_call_chunk (early signal for the UI); the "updates"
            # path emits the final args. Same tool_call_id → emit once.
            announced_tool_ids: set[str] = set()

            # Stream using LangGraph's async streaming. Hold the stream in
            # a variable so the ``finally`` block below can call
            # ``aclose()`` on it. LangGraph's astream is backed by
            # ``asyncio.Queue`` readers spawned as background tasks;
            # without an explicit aclose, those readers stay pending and
            # get destroyed by GC on the next run, surfacing as
            # ``Task was destroyed but it is pending! Queue.get()`` log
            # noise around backend transitions.
            _stream = agent.astream(
                {"messages": messages},
                stream_mode=["updates", "messages"],
                version="v2",
                config=config if config else None,
            )
            async for chunk in _stream:
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
                    text, thinking = _split_content_text_and_thinking(
                        getattr(msg_chunk, "content", "")
                    )
                    if thinking:
                        yield AgentEvent(type="thinking", content=thinking)
                    if text:
                        yield AgentEvent(type="message", content=text)

                    # Early tool_use signal — fire as soon as the first
                    # tool_call_chunk carries a name. The UI flips from
                    # "Thinking..." to "Using <tool>..." 1-2s sooner than
                    # waiting for the full tool_call in the updates path.
                    tool_call_chunks = getattr(msg_chunk, "tool_call_chunks", None) or []
                    for tcc in tool_call_chunks:
                        # ToolCallChunk may be a dict or a Pydantic model.
                        tcc_name = (
                            tcc.get("name") if isinstance(tcc, dict) else getattr(tcc, "name", None)
                        )
                        tcc_id = (
                            tcc.get("id") if isinstance(tcc, dict) else getattr(tcc, "id", None)
                        )
                        if not tcc_name or not tcc_id:
                            continue
                        if tcc_id in announced_tool_ids:
                            continue
                        announced_tool_ids.add(tcc_id)
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Using {tcc_name}...",
                            metadata={"name": tcc_name, "input": {}},
                        )

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
                                    tc_id = tc.get("id")
                                    # Skip if the messages-path already
                                    # announced this tool. Still emit
                                    # when id is missing (some providers
                                    # omit it) so behavior degrades to
                                    # the old single-emit path.
                                    if tc_id and tc_id in announced_tool_ids:
                                        continue
                                    if tc_id:
                                        announced_tool_ids.add(tc_id)
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
        finally:
            # Close the astream generator so LangGraph's background
            # Queue readers are cancelled cleanly. Without this, those
            # readers GC with a pending Queue.get() at the next backend
            # transition. Idempotent on streams that already exited.
            if _stream is not None:
                close = getattr(_stream, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "astream aclose error (non-fatal): %s", exc
                        )

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
