"""Tests for Deep Agents backend -- mocked (no real SDK needed)."""

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


class TestDeepAgentsBackendInfo:
    """Tests for static backend metadata."""

    def test_info_name(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.name == "deep_agents"

    def test_info_display_name(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.display_name == "Deep Agents (LangChain)"

    def test_info_capabilities(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities

    def test_info_beta(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.beta is True

    def test_info_install_hint(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        info = DeepAgentsBackend.info()
        assert info.install_hint["pip_spec"] == "pocketpaw[deep-agents]"
        assert info.install_hint["verify_import"] == "deepagents"


class TestDeepAgentsProviderParsing:
    """Tests for provider:model parsing and resolution."""

    def test_parse_anthropic_colon_format(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="anthropic:claude-sonnet-4-6")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"

    def test_parse_openai_colon_format(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="openai:gpt-4o")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "openai"
        assert model == "gpt-4o"

    def test_parse_ollama_colon_format(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="ollama:llama3.2")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "ollama"
        assert model == "llama3.2"

    def test_parse_google_genai_colon_format(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="google_genai:gemini-2.0-flash")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "google_genai"
        assert model == "gemini-2.0-flash"

    def test_parse_model_only_defaults_to_anthropic(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="claude-sonnet-4-6", llm_provider="auto")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"

    def test_parse_empty_model_defaults(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="", llm_provider="auto")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"

    def test_parse_litellm_colon_format(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="litellm:anthropic/claude-sonnet-4-6")
        backend = DeepAgentsBackend(settings)
        provider, model = backend._parse_provider_model()
        assert provider == "litellm"
        assert model == "anthropic/claude-sonnet-4-6"


class TestDeepAgentsUnwrap:
    """Tests for _unwrap helper that handles LangGraph Overwrite objects."""

    def test_unwrap_plain_value(self):
        from pocketpaw.agents.deep_agents import _unwrap

        assert _unwrap([1, 2, 3]) == [1, 2, 3]
        assert _unwrap({"key": "val"}) == {"key": "val"}
        assert _unwrap("hello") == "hello"

    def test_unwrap_overwrite_object(self):
        from unittest.mock import MagicMock

        from pocketpaw.agents.deep_agents import _unwrap

        # Simulate LangGraph Overwrite object which has a .value attribute
        overwrite = MagicMock()
        overwrite.value = [{"role": "assistant", "content": "hi"}]
        assert _unwrap(overwrite) == [{"role": "assistant", "content": "hi"}]

    def test_unwrap_none(self):
        from pocketpaw.agents.deep_agents import _unwrap

        assert _unwrap(None) is None


class TestDeepAgentsContentExtraction:
    """Tests for _extract_content_text helper."""

    def test_string_content(self):
        from pocketpaw.agents.deep_agents import _extract_content_text

        assert _extract_content_text("hello") == "hello"

    def test_list_content_text_blocks(self):
        from pocketpaw.agents.deep_agents import _extract_content_text

        content = [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]
        assert _extract_content_text(content) == "hello world"

    def test_list_content_mixed_blocks(self):
        from pocketpaw.agents.deep_agents import _extract_content_text

        content = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "123", "name": "test"},
        ]
        assert _extract_content_text(content) == "hello"

    def test_list_content_plain_strings(self):
        from pocketpaw.agents.deep_agents import _extract_content_text

        assert _extract_content_text(["hello ", "world"]) == "hello world"

    def test_empty_content(self):
        from pocketpaw.agents.deep_agents import _extract_content_text

        assert _extract_content_text("") == ""
        assert _extract_content_text([]) == ""
        assert _extract_content_text(None) == ""


class TestDeepAgentsBackendInit:
    """Tests for backend initialization."""

    def test_custom_tools_cached(self):
        """_build_custom_tools caches the result."""
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        mock_tools = [MagicMock(), MagicMock()]

        with patch(
            "pocketpaw.agents.tool_bridge.build_deep_agents_tools",
            return_value=mock_tools,
        ):
            backend._custom_tools = None
            result1 = backend._build_custom_tools()
            result2 = backend._build_custom_tools()
            assert result1 is result2

    def test_custom_tools_graceful_degradation(self):
        """Returns empty list when tool_bridge is unavailable."""
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        with patch.dict("sys.modules", {"pocketpaw.agents.tool_bridge": None}):
            backend._custom_tools = None
            result = backend._build_custom_tools()
            assert result == []


class TestDeepAgentsBackendRun:
    """Tests for the run() async generator."""

    @pytest.mark.asyncio
    async def test_run_sdk_unavailable_yields_error(self):
        """When SDK is missing, run() yields an error event."""
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        backend._sdk_available = False

        events = []
        async for event in backend.run("hello"):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"
        assert "not installed" in events[0].content.lower()

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        assert backend._stop_flag is False
        await backend.stop()
        assert backend._stop_flag is True

    @pytest.mark.asyncio
    async def test_get_status(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        status = await backend.get_status()
        assert status["backend"] == "deep_agents"
        assert "available" in status
        assert "running" in status
        assert "model" in status
        assert "provider" in status
        assert "resolved_model" in status

    @pytest.mark.asyncio
    async def test_get_status_shows_resolved_provider(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        settings = Settings(deep_agents_model="ollama:codellama")
        backend = DeepAgentsBackend(settings)
        status = await backend.get_status()
        assert status["provider"] == "ollama"
        assert status["resolved_model"] == "codellama"


class TestDeepAgentsRegistry:
    """Tests for registry integration."""

    def test_backend_in_registry(self):
        from pocketpaw.agents.registry import list_backends

        assert "deep_agents" in list_backends()

    def test_backend_class_loadable(self):
        from pocketpaw.agents.registry import get_backend_class

        cls = get_backend_class("deep_agents")
        # May be None if deepagents not installed, but should not raise
        if cls is not None:
            info = cls.info()
            assert info.name == "deep_agents"
