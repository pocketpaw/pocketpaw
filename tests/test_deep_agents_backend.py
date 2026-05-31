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


class TestDeepAgentsResponsesApiKwarg:
    """init_chat_model must receive use_responses_api=False for OpenAI-compat
    endpoints that don't speak the OpenAI Responses API (DeepSeek, OpenRouter,
    LiteLLM, vLLM). With deepagents 0.5.x defaulting to Responses, omitting
    this kwarg sends every call to a 404."""

    def _capture_init_chat_model(self, settings: Settings) -> tuple[str, dict]:
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(settings)
        captured: dict[str, object] = {}

        def fake_init(model_id: str, **kwargs):
            captured["model_id"] = model_id
            captured["kwargs"] = kwargs
            return MagicMock()

        with patch("langchain.chat_models.init_chat_model", side_effect=fake_init):
            backend._build_model()
        return str(captured["model_id"]), dict(captured["kwargs"])  # type: ignore[arg-type]

    def test_openai_compatible_forces_chat_completions(self):
        settings = Settings(
            deep_agents_model="openai_compatible:deepseek-v4-pro",
            openai_compatible_base_url="https://api.deepseek.com/v1",
            openai_compatible_api_key="sk-test",
        )
        model_id, kwargs = self._capture_init_chat_model(settings)
        assert kwargs.get("use_responses_api") is False
        assert kwargs.get("base_url") == "https://api.deepseek.com/v1"
        assert model_id == "openai:deepseek-v4-pro"

    def test_openrouter_forces_chat_completions(self):
        settings = Settings(
            deep_agents_model="openrouter:deepseek/deepseek-v4-pro",
            openrouter_api_key="sk-or-test",
        )
        _model_id, kwargs = self._capture_init_chat_model(settings)
        assert kwargs.get("use_responses_api") is False

    def test_litellm_uses_native_chatlitellm(self):
        # The litellm branch now routes through the native ChatLiteLLM
        # integration (langchain-litellm) so the LiteLLM SDK can handle
        # provider-specific quirks (DeepSeek reasoning_content, Anthropic
        # thinking blocks, etc.). ChatLiteLLM uses ``api_base`` (not
        # ``base_url``) and does NOT accept ``use_responses_api``.
        settings = Settings(
            deep_agents_model="litellm:claude-sonnet-4-6",
            litellm_api_base="http://proxy:4000",
            litellm_api_key="sk-test",
        )
        model_id, kwargs = self._capture_init_chat_model(settings)
        assert model_id == "litellm:claude-sonnet-4-6"
        assert kwargs.get("api_base") == "http://proxy:4000"
        assert kwargs.get("api_key") == "sk-test"
        assert "use_responses_api" not in kwargs
        assert "base_url" not in kwargs

    def test_plain_openai_keeps_responses_api_default(self):
        # No custom base_url — talking to api.openai.com directly. We must
        # NOT force chat-completions or we lose Responses-API features.
        settings = Settings(
            deep_agents_model="openai:gpt-4o",
            openai_api_key="sk-test",
        )
        _model_id, kwargs = self._capture_init_chat_model(settings)
        assert "use_responses_api" not in kwargs

    def test_anthropic_unaffected(self):
        settings = Settings(
            deep_agents_model="anthropic:claude-sonnet-4-6",
            anthropic_api_key="sk-ant-test",
        )
        _model_id, kwargs = self._capture_init_chat_model(settings)
        assert "use_responses_api" not in kwargs


class TestDeepAgentsSkillsMemoryPlumbing:
    """skills= / memory= settings must reach create_deep_agent() only when
    populated, and must invalidate the compiled-graph cache when changed."""

    def _capture_create_deep_agent(self, settings: Settings):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(settings)
        backend._custom_tools = []
        captured: dict[str, object] = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return MagicMock(name="compiled_agent")

        with patch("deepagents.create_deep_agent", side_effect=fake_create):
            backend._get_or_create_agent(MagicMock(), "system")
        return captured

    def test_skills_forwarded_when_populated(self):
        settings = Settings(deep_agents_skills=["/skills/ripple", "/skills/pockets"])
        captured = self._capture_create_deep_agent(settings)
        assert captured.get("skills") == ["/skills/ripple", "/skills/pockets"]

    def test_memory_forwarded_when_populated(self):
        settings = Settings(deep_agents_memory=["/mem/AGENTS.md"])
        captured = self._capture_create_deep_agent(settings)
        assert captured.get("memory") == ["/mem/AGENTS.md"]

    def test_skills_omitted_when_empty(self):
        # Passing an empty list still wires SkillsMiddleware with nothing to
        # load — wasted middleware. Better to not forward at all.
        captured = self._capture_create_deep_agent(Settings())
        assert "skills" not in captured
        assert "memory" not in captured

    def test_cache_key_includes_skills(self):
        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings(deep_agents_skills=["/a"]))
        backend._custom_tools = []
        first = MagicMock(name="first")
        second = MagicMock(name="second")

        with patch("deepagents.create_deep_agent", side_effect=[first, second]):
            agent1 = backend._get_or_create_agent(MagicMock(), "sys")
            # Same settings → cached
            agent_again = backend._get_or_create_agent(MagicMock(), "sys")
            assert agent1 is agent_again
            # Mutate skills → cache invalidates, second instance compiled
            backend.settings = Settings(deep_agents_skills=["/a", "/b"])
            agent2 = backend._get_or_create_agent(MagicMock(), "sys")
            assert agent2 is not agent1


class TestDeepAgentsAttachSpecialistTools:
    """attach_specialist_tools() merges into _custom_tools and invalidates the
    compiled-graph cache so the next run picks up the new tool surface."""

    def test_appends_tools_and_invalidates_cache(self):
        from langchain_core.tools import StructuredTool

        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        backend._custom_tools = [MagicMock(name="existing")]
        backend._cached_agent = MagicMock(name="prev")
        backend._cached_model_key = ("anthropic:x", (), ())

        new_tool = StructuredTool.from_function(func=lambda: "hi", name="extra", description="x")
        backend.attach_specialist_tools([new_tool])

        assert backend._custom_tools[-1] is new_tool
        assert backend._cached_agent is None  # cache invalidated
        assert backend._cached_model_key is None  # both halves of the cache cleared

    def test_skips_mcp_loading(self):
        from langchain_core.tools import StructuredTool

        from pocketpaw.agents.deep_agents import DeepAgentsBackend

        backend = DeepAgentsBackend(Settings())
        backend._mcp_tools = None  # default state where _build_mcp_tools would load
        backend._custom_tools = None

        tool = StructuredTool.from_function(func=lambda: "x", name="t", description="x")
        backend.attach_specialist_tools([tool])

        # _mcp_tools is now [] — _build_mcp_tools() short-circuits and never
        # connects to user MCP servers.
        assert backend._mcp_tools == []


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
