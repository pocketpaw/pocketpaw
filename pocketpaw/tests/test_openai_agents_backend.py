"""Tests for OpenAI Agents SDK backend — mocked (no real SDK needed)."""

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


class TestOpenAIAgentsCustomTools:
    """Tests for PocketPaw custom tool wiring."""

    def test_custom_tools_cached(self):
        """_build_custom_tools caches the result."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        mock_tools = [MagicMock(), MagicMock()]

        with patch.dict(
            "sys.modules",
            {
                "pocketpaw.agents.tool_bridge": MagicMock(
                    build_openai_function_tools=MagicMock(return_value=mock_tools)
                )
            },
        ):
            result1 = backend._build_custom_tools()
            result2 = backend._build_custom_tools()
            # Should be the same cached list
            assert result1 is result2

    def test_custom_tools_graceful_degradation(self):
        """Returns empty list when tool_bridge is unavailable."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        # Ensure tool_bridge import fails
        with patch.dict("sys.modules", {"pocketpaw.agents.tool_bridge": None}):
            # Reset cache
            backend._custom_tools = None
            result = backend._build_custom_tools()
            assert result == []

    @pytest.mark.asyncio
    async def test_agent_created_with_tools(self):
        """Agent constructor receives tools= parameter from _build_custom_tools."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        backend._sdk_available = True
        backend._sqlite_session_available = False

        mock_tool = MagicMock()
        backend._custom_tools = [mock_tool]

        captured_tools = None
        mock_result = MagicMock()

        async def empty_stream():
            return
            yield  # noqa: E711

        mock_result.stream_events = empty_stream

        with patch.dict(
            "sys.modules",
            {
                "agents": MagicMock(),
                "openai": MagicMock(),
                "openai.types": MagicMock(),
                "openai.types.responses": MagicMock(),
            },
        ):
            import sys

            mock_agents = sys.modules["agents"]
            mock_agent_cls = MagicMock()
            mock_agents.Agent = mock_agent_cls

            def capture_agent(**kwargs):
                nonlocal captured_tools
                captured_tools = kwargs.get("tools")
                return MagicMock()

            mock_agent_cls.side_effect = capture_agent
            mock_agents.Runner.run_streamed = MagicMock(return_value=mock_result)

            async for _ in backend.run("test"):
                pass

            assert captured_tools is not None
            assert mock_tool in captured_tools


class TestOpenAIAgentsInfo:
    def test_info_static(self):
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        info = OpenAIAgentsBackend.info()
        assert info.name == "openai_agents"
        assert info.display_name == "OpenAI Agents SDK"
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert "code_interpreter" in info.builtin_tools

    def test_tool_policy_map(self):
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        info = OpenAIAgentsBackend.info()
        assert info.tool_policy_map["code_interpreter"] == "shell"

    def test_required_keys_and_providers(self):
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        info = OpenAIAgentsBackend.info()
        assert "openai_api_key" in info.required_keys
        assert "openai" in info.supported_providers
        assert "ollama" in info.supported_providers
        assert "openai_compatible" in info.supported_providers


class TestOpenAIAgentsProvider:
    """Tests for per-backend provider selection (openai_agents_provider)."""

    def test_build_model_uses_per_backend_provider(self):
        """openai_agents_provider takes precedence over llm_provider."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        settings = Settings()
        settings.llm_provider = "anthropic"  # global — should be ignored
        settings.openai_agents_provider = "openai"  # per-backend — should win
        backend = OpenAIAgentsBackend(settings)
        # With provider="openai", _build_model should return a string (model name)
        model = backend._build_model()
        assert isinstance(model, str)  # Not an OpenAIChatCompletionsModel

    def test_build_model_ollama_via_per_backend_provider(self):
        """openai_agents_provider=ollama creates OpenAIChatCompletionsModel."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        settings = Settings()
        settings.llm_provider = "openai"  # global — should be ignored
        settings.openai_agents_provider = "ollama"  # per-backend
        settings.ollama_host = "http://localhost:11434"
        settings.ollama_model = "llama3.2"
        backend = OpenAIAgentsBackend(settings)

        try:
            model = backend._build_model()
            # If openai-agents is installed, this should return OpenAIChatCompletionsModel
            assert not isinstance(model, str)
        except (ImportError, ModuleNotFoundError):
            # SDK not installed — that's OK, the important thing is the provider was respected
            pass

    def test_build_model_falls_back_to_llm_provider(self):
        """When openai_agents_provider is empty, falls back to llm_provider."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        settings = Settings()
        settings.openai_agents_provider = ""
        settings.llm_provider = "openai"
        backend = OpenAIAgentsBackend(settings)
        model = backend._build_model()
        assert isinstance(model, str)  # String model name for standard OpenAI


class TestOpenAIAgentsInit:
    def test_init_without_sdk(self):
        """Should initialize even without the SDK installed."""
        with patch.dict("sys.modules", {"agents": None}):
            from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

            backend = OpenAIAgentsBackend(Settings())
            # May or may not be available depending on test env
            assert backend is not None

    @pytest.mark.asyncio
    async def test_run_without_sdk(self):
        """Should yield error if SDK not available."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        backend._sdk_available = False

        events = []
        async for event in backend.run("test"):
            events.append(event)

        assert any(e.type == "error" for e in events)

    @pytest.mark.asyncio
    async def test_stop(self):
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        await backend.stop()
        assert backend._stop_flag is True

    @pytest.mark.asyncio
    async def test_get_status(self):
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        status = await backend.get_status()
        assert status["backend"] == "openai_agents"
        assert "available" in status
        assert "native_sessions" in status
        assert "active_sessions" in status


class TestOpenAIAgentsSessions:
    """Tests for native SQLiteSession integration."""

    def test_session_created_for_key(self):
        """SQLiteSession is created and cached for a given session_key."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        backend._sqlite_session_available = True

        mock_session = MagicMock()
        with patch(
            "pocketpaw.agents.openai_agents.SQLiteSession",
            create=True,
        ) as mock_cls:
            # Patch the import inside _get_or_create_session
            with patch.dict(
                "sys.modules",
                {
                    "agents.extensions.persistence": MagicMock(
                        SQLiteSession=mock_cls,
                    ),
                },
            ):
                mock_cls.return_value = mock_session
                session = backend._get_or_create_session("test-session-1")
                assert session is mock_session
                assert "test-session-1" in backend._sessions

    def test_session_reused(self):
        """Same session_key returns the same cached session."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        backend._sqlite_session_available = True

        mock_session = MagicMock()
        backend._sessions["test-key"] = mock_session

        result = backend._get_or_create_session("test-key")
        assert result is mock_session

    def test_inject_history_helper(self):
        """_inject_history appends history to instructions."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = OpenAIAgentsBackend._inject_history("Base prompt.", history)
        assert "Base prompt." in result
        assert "# Recent Conversation" in result
        assert "**User**: Hello" in result
        assert "**Assistant**: Hi!" in result

    def test_inject_history_truncates_long_messages(self):
        """_inject_history truncates messages over 500 chars."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        long_msg = "x" * 600
        history = [{"role": "user", "content": long_msg}]
        result = OpenAIAgentsBackend._inject_history("Base.", history)
        assert "x" * 500 + "..." in result
        assert "x" * 501 not in result


class TestOpenAIAgentsSessionReuse:
    """Tests for session reuse and cross-backend portability."""

    def _make_backend_with_mocks(self):
        """Create a backend with SDK mocked out, ready for run() calls."""
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend

        backend = OpenAIAgentsBackend(Settings())
        backend._sdk_available = True
        backend._sqlite_session_available = True
        return backend

    def _mock_sdk_context(self):
        """Return a context manager that patches the SDK modules."""
        return patch.dict(
            "sys.modules",
            {
                "agents": MagicMock(),
                "agents.extensions": MagicMock(),
                "agents.extensions.persistence": MagicMock(),
                "openai": MagicMock(),
                "openai.types": MagicMock(),
                "openai.types.responses": MagicMock(),
            },
        )

    @pytest.mark.asyncio
    async def test_history_not_injected_on_existing_session(self):
        """When session already exists (not first call), history is NOT injected."""
        backend = self._make_backend_with_mocks()

        mock_session = MagicMock()
        backend._sessions["s1"] = mock_session  # Pre-existing session

        captured_kwargs = {}

        mock_result = MagicMock()

        async def empty_stream():
            return
            yield  # noqa: E711

        mock_result.stream_events = empty_stream

        with self._mock_sdk_context():
            import sys

            mock_agents = sys.modules["agents"]
            mock_agent_cls = MagicMock()
            mock_agents.Agent = mock_agent_cls

            agent_instance = MagicMock(instructions="base")

            def capture_agent(**kwargs):
                agent_instance.instructions = kwargs.get("instructions", "")
                return agent_instance

            mock_agent_cls.side_effect = capture_agent

            def capture_run_streamed(agent, **kwargs):
                captured_kwargs.update(kwargs)
                return mock_result

            mock_agents.Runner.run_streamed = capture_run_streamed

            history = [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ]

            events = []
            async for event in backend.run(
                "What's up?",
                system_prompt="You are PocketPaw.",
                history=history,
                session_key="s1",
            ):
                events.append(event)

            # Session already existed — history should NOT be injected
            assert "Recent Conversation" not in agent_instance.instructions
            # Native session should be passed
            assert captured_kwargs.get("session") is mock_session

    @pytest.mark.asyncio
    async def test_history_seeded_on_new_session(self):
        """First call with a new session_key seeds history (cross-backend portability)."""
        backend = self._make_backend_with_mocks()
        # _sessions is empty — "s1" is new

        captured_instructions = None
        captured_kwargs = {}

        mock_result = MagicMock()

        async def empty_stream():
            return
            yield  # noqa: E711

        mock_result.stream_events = empty_stream
        mock_session = MagicMock()

        with self._mock_sdk_context():
            import sys

            mock_agents = sys.modules["agents"]
            mock_agent_cls = MagicMock()
            mock_agents.Agent = mock_agent_cls

            # Mock _get_or_create_session to add to _sessions
            mock_persistence = sys.modules["agents.extensions.persistence"]
            mock_persistence.SQLiteSession.return_value = mock_session

            def capture_agent(**kwargs):
                nonlocal captured_instructions
                captured_instructions = kwargs.get("instructions", "")
                return MagicMock(instructions=captured_instructions)

            mock_agent_cls.side_effect = capture_agent

            def capture_run_streamed(agent, **kwargs):
                captured_kwargs.update(kwargs)
                return mock_result

            mock_agents.Runner.run_streamed = capture_run_streamed

            history = [
                {"role": "user", "content": "From previous backend"},
                {"role": "assistant", "content": "I remember that"},
            ]

            events = []
            async for event in backend.run(
                "Continue our chat",
                system_prompt="You are PocketPaw.",
                history=history,
                session_key="s1",
            ):
                events.append(event)

            # New session — history SHOULD be seeded into instructions
            assert captured_instructions is not None
            assert "Recent Conversation" in captured_instructions
            assert "From previous backend" in captured_instructions
            # Native session should still be passed
            assert "session" in captured_kwargs

    @pytest.mark.asyncio
    async def test_second_call_skips_history_after_seed(self):
        """After first call seeds history, second call with same key skips it."""
        backend = self._make_backend_with_mocks()

        captured_instructions_list = []

        mock_result = MagicMock()

        async def empty_stream():
            return
            yield  # noqa: E711

        mock_result.stream_events = empty_stream
        mock_session = MagicMock()

        with self._mock_sdk_context():
            import sys

            mock_agents = sys.modules["agents"]
            mock_agent_cls = MagicMock()
            mock_agents.Agent = mock_agent_cls

            mock_persistence = sys.modules["agents.extensions.persistence"]
            mock_persistence.SQLiteSession.return_value = mock_session

            def capture_agent(**kwargs):
                captured_instructions_list.append(kwargs.get("instructions", ""))
                return MagicMock(instructions=kwargs.get("instructions", ""))

            mock_agent_cls.side_effect = capture_agent
            mock_agents.Runner.run_streamed = MagicMock(return_value=mock_result)

            history = [
                {"role": "user", "content": "context from memory"},
            ]

            # First call — should seed
            async for _ in backend.run(
                "first msg", system_prompt="Base.", history=history, session_key="key1"
            ):
                pass

            # Second call — same key, should NOT inject
            async for _ in backend.run(
                "second msg", system_prompt="Base.", history=history, session_key="key1"
            ):
                pass

            assert len(captured_instructions_list) == 2
            # First call: seeded
            assert "Recent Conversation" in captured_instructions_list[0]
            # Second call: NOT seeded
            assert "Recent Conversation" not in captured_instructions_list[1]

    @pytest.mark.asyncio
    async def test_fallback_without_session_key(self):
        """Without session_key, history is always injected (fallback)."""
        backend = self._make_backend_with_mocks()

        captured_instructions = None
        captured_kwargs = {}

        mock_result = MagicMock()

        async def empty_stream():
            return
            yield  # noqa: E711

        mock_result.stream_events = empty_stream

        with self._mock_sdk_context():
            import sys

            mock_agents = sys.modules["agents"]
            mock_agent_cls = MagicMock()
            mock_agents.Agent = mock_agent_cls

            def capture_agent(**kwargs):
                nonlocal captured_instructions
                captured_instructions = kwargs.get("instructions", "")
                return MagicMock(instructions=captured_instructions)

            mock_agent_cls.side_effect = capture_agent

            def capture_run_streamed(agent, **kwargs):
                captured_kwargs.update(kwargs)
                return mock_result

            mock_agents.Runner.run_streamed = capture_run_streamed

            history = [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ]

            events = []
            async for event in backend.run(
                "What's up?",
                system_prompt="You are PocketPaw.",
                history=history,
                # No session_key — should fall back to history injection
            ):
                events.append(event)

            # Verify history was injected into instructions
            assert captured_instructions is not None
            assert "Recent Conversation" in captured_instructions
            assert "Hello" in captured_instructions

            # Verify no session was passed
            assert "session" not in captured_kwargs
