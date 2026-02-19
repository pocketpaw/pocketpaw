"""Tests for Copilot SDK backend — mocked (no real CLI/SDK needed)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


class TestCopilotSDKInfo:
    def test_info_static(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        info = CopilotSDKBackend.info()
        assert info.name == "copilot_sdk"
        assert info.display_name == "Copilot SDK"
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities
        assert Capability.MCP not in info.capabilities
        assert "shell" in info.builtin_tools
        assert "web_search" in info.builtin_tools

    def test_tool_policy_map(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        info = CopilotSDKBackend.info()
        assert info.tool_policy_map["shell"] == "shell"
        assert info.tool_policy_map["file_ops"] == "write_file"
        assert info.tool_policy_map["web_search"] == "browser"

    def test_required_keys_empty(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        info = CopilotSDKBackend.info()
        assert info.required_keys == []

    def test_supported_providers(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        info = CopilotSDKBackend.info()
        assert "copilot" in info.supported_providers
        assert "openai" in info.supported_providers
        assert "azure" in info.supported_providers
        assert "anthropic" in info.supported_providers


class TestCopilotSDKInit:
    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_init_with_cli_and_sdk(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            backend = CopilotSDKBackend(Settings())
            assert backend._cli_available is True
            assert backend._sdk_available is True

    @patch("shutil.which", return_value=None)
    def test_init_without_cli(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            backend = CopilotSDKBackend(Settings())
            assert backend._cli_available is False

    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_init_without_sdk(self, mock_which):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        # Manually construct to simulate missing SDK
        backend = CopilotSDKBackend.__new__(CopilotSDKBackend)
        backend.settings = Settings()
        backend._stop_flag = False
        backend._cli_available = True
        backend._sdk_available = False
        backend._client = None
        backend._sessions = {}
        assert backend._sdk_available is False

    @pytest.mark.asyncio
    @patch("shutil.which", return_value=None)
    async def test_run_without_cli(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            backend = CopilotSDKBackend(Settings())
            events = []
            async for event in backend.run("test"):
                events.append(event)

            assert any(e.type == "error" for e in events)
            assert any("not found" in e.content for e in events if e.type == "error")

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/copilot")
    async def test_run_without_sdk(self, mock_which):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        backend = CopilotSDKBackend.__new__(CopilotSDKBackend)
        backend.settings = Settings()
        backend._stop_flag = False
        backend._cli_available = True
        backend._sdk_available = False
        backend._client = None
        backend._sessions = {}

        events = []
        async for event in backend.run("test"):
            events.append(event)

        assert any(e.type == "error" for e in events)
        assert any("github-copilot-sdk" in e.content for e in events if e.type == "error")

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/copilot")
    async def test_stop(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            backend = CopilotSDKBackend(Settings())
            await backend.stop()
            assert backend._stop_flag is True
            assert backend._client is None

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/copilot")
    async def test_get_status(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            backend = CopilotSDKBackend(Settings())
            status = await backend.get_status()
            assert status["backend"] == "copilot_sdk"
            assert status["cli_available"] is True
            assert status["sdk_available"] is True
            assert "model" in status
            assert "provider" in status


class TestCopilotSDKHelpers:
    def test_inject_history(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = CopilotSDKBackend._inject_history("Base prompt.", history)
        assert "Base prompt." in result
        assert "# Recent Conversation" in result
        assert "**User**: Hello" in result
        assert "**Assistant**: Hi!" in result

    def test_inject_history_truncates(self):
        from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

        long_msg = "x" * 600
        history = [{"role": "user", "content": long_msg}]
        result = CopilotSDKBackend._inject_history("Base.", history)
        assert "x" * 500 + "..." in result
        assert "x" * 501 not in result

    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_get_provider_config_copilot(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            backend = CopilotSDKBackend(Settings())
            config = backend._get_provider_config()
            assert config is None  # Default: GitHub OAuth, no BYOK config

    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_get_provider_config_openai(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            settings = Settings(copilot_sdk_provider="openai", openai_api_key="sk-test")
            backend = CopilotSDKBackend(settings)
            config = backend._get_provider_config()
            assert config is not None
            assert config["type"] == "openai"
            assert config["api_key"] == "sk-test"

    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_get_provider_config_azure(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            settings = Settings(
                copilot_sdk_provider="azure",
                openai_compatible_base_url="https://my.openai.azure.com",
                openai_api_key="az-key",
            )
            backend = CopilotSDKBackend(settings)
            config = backend._get_provider_config()
            assert config is not None
            assert config["type"] == "azure"
            assert config["base_url"] == "https://my.openai.azure.com"
            assert config["api_key"] == "az-key"

    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_get_provider_config_anthropic(self, mock_which):
        with patch.dict("sys.modules", {"copilot": MagicMock()}):
            from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

            settings = Settings(
                copilot_sdk_provider="anthropic",
                anthropic_api_key="sk-ant-test",
            )
            backend = CopilotSDKBackend(settings)
            config = backend._get_provider_config()
            assert config is not None
            assert config["type"] == "anthropic"
            assert config["api_key"] == "sk-ant-test"


def _make_sdk_event(event_type: str, **kwargs) -> MagicMock:
    """Create a mock Copilot SDK event with enum-style type and data object."""
    event = MagicMock()
    # event.type is an enum with .value
    type_enum = MagicMock()
    type_enum.value = event_type
    event.type = type_enum
    # event.data holds the payload attributes
    data = MagicMock(spec=[])
    for k, v in kwargs.items():
        setattr(data, k, v)
    event.data = data
    return event


def _setup_backend_with_mock_client():
    """Create a CopilotSDKBackend with a fully mocked client."""
    from pocketpaw.agents.copilot_sdk import CopilotSDKBackend

    backend = CopilotSDKBackend.__new__(CopilotSDKBackend)
    backend.settings = Settings()
    backend._stop_flag = False
    backend._cli_available = True
    backend._sdk_available = True
    backend._sessions = {}

    mock_session = MagicMock()
    mock_session.on = MagicMock()
    mock_session.send = AsyncMock()
    mock_session.destroy = AsyncMock()

    mock_client = MagicMock()
    mock_client.start = AsyncMock()
    mock_client.stop = AsyncMock()
    mock_client.create_session = AsyncMock(return_value=mock_session)
    backend._client = mock_client

    return backend, mock_session, mock_client


def _wire_events(mock_session, events_to_fire):
    """Set up mock_session.on to fire the given events asynchronously."""

    def capture_on(handler):
        async def fire():
            await asyncio.sleep(0.01)
            for ev in events_to_fire:
                handler(ev)

        asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(fire()))

    mock_session.on.side_effect = capture_on


class TestCopilotSDKRun:
    @pytest.mark.asyncio
    async def test_maps_message_delta(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        _wire_events(
            mock_session,
            [
                _make_sdk_event("assistant.message_delta", delta_content="Hello!"),
                _make_sdk_event("session.idle"),
            ],
        )

        events = []
        async for event in backend.run("Hi"):
            events.append(event)

        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 1
        assert messages[0].content == "Hello!"
        assert events[-1].type == "done"

    @pytest.mark.asyncio
    async def test_maps_thinking_delta(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        _wire_events(
            mock_session,
            [
                _make_sdk_event("assistant.reasoning_delta", delta_content="Thinking..."),
                _make_sdk_event("session.idle"),
            ],
        )

        events = []
        async for event in backend.run("Think"):
            events.append(event)

        thinking = [e for e in events if e.type == "thinking"]
        assert len(thinking) == 1
        assert "Thinking" in thinking[0].content

    @pytest.mark.asyncio
    async def test_maps_complete_message(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        _wire_events(
            mock_session,
            [
                _make_sdk_event("assistant.message", content="Final answer."),
                _make_sdk_event("session.idle"),
            ],
        )

        events = []
        async for event in backend.run("Question"):
            events.append(event)

        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 1
        assert messages[0].content == "Final answer."

    @pytest.mark.asyncio
    async def test_maps_tool_call_and_result(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        _wire_events(
            mock_session,
            [
                _make_sdk_event("tool.call", name="shell", arguments={"command": "ls"}),
                _make_sdk_event("tool.result", name="shell", output="file.txt"),
                _make_sdk_event("assistant.message_delta", delta_content="Found file.txt"),
                _make_sdk_event("session.idle"),
            ],
        )

        events = []
        async for event in backend.run("list files"):
            events.append(event)

        tool_use = [e for e in events if e.type == "tool_use"]
        tool_result = [e for e in events if e.type == "tool_result"]
        assert len(tool_use) == 1
        assert tool_use[0].metadata["name"] == "shell"
        assert tool_use[0].metadata["input"] == {"command": "ls"}
        assert len(tool_result) == 1
        assert tool_result[0].metadata["name"] == "shell"

    @pytest.mark.asyncio
    async def test_maps_error_event(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        _wire_events(
            mock_session,
            [
                _make_sdk_event("error", message="Rate limit exceeded"),
                _make_sdk_event("session.idle"),
            ],
        )

        events = []
        async for event in backend.run("test"):
            events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 1
        assert "Rate limit" in errors[0].content

    @pytest.mark.asyncio
    async def test_max_turns_limit(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        backend.settings = Settings(copilot_sdk_max_turns=2)

        _wire_events(
            mock_session,
            [
                _make_sdk_event("tool.result", name="t0", output="ok"),
                _make_sdk_event("tool.result", name="t1", output="ok"),
                _make_sdk_event("tool.result", name="t2", output="ok"),
                _make_sdk_event("session.idle"),
            ],
        )

        events = []
        async for event in backend.run("test"):
            events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert any("max turns" in e.content.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_sends_prompt_as_dict(self):
        """Verify session.send() receives a dict with 'prompt' key."""
        backend, mock_session, _ = _setup_backend_with_mock_client()
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        async for _ in backend.run("Hello"):
            pass

        mock_session.send.assert_called_once()
        call_arg = mock_session.send.call_args[0][0]
        assert isinstance(call_arg, dict)
        assert "prompt" in call_arg
        assert "Hello" in call_arg["prompt"]

    @pytest.mark.asyncio
    async def test_create_session_receives_dict(self):
        """Verify create_session() receives a dict (not kwargs)."""
        backend, mock_session, mock_client = _setup_backend_with_mock_client()
        # Force new session creation by clearing sessions and client cache
        backend._sessions.clear()
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        async for _ in backend.run("Hello", session_key="new"):
            pass

        mock_client.create_session.assert_called_once()
        call_arg = mock_client.create_session.call_args[0][0]
        assert isinstance(call_arg, dict)
        assert call_arg["streaming"] is True
        assert "model" in call_arg


class TestCopilotSDKCrossBackend:
    @pytest.mark.asyncio
    async def test_history_injected_into_prompt(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        captured_prompt = None

        async def capture_send(msg):
            nonlocal captured_prompt
            captured_prompt = msg.get("prompt", "") if isinstance(msg, dict) else msg

        mock_session.send = capture_send
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        history = [
            {"role": "user", "content": "From previous backend"},
            {"role": "assistant", "content": "I remember"},
        ]

        async for _ in backend.run(
            "Continue",
            system_prompt="You are PocketPaw.",
            history=history,
            session_key="s1",
        ):
            pass

        assert captured_prompt is not None
        assert "Recent Conversation" in captured_prompt
        assert "From previous backend" in captured_prompt

    @pytest.mark.asyncio
    async def test_system_prompt_injected(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        captured_prompt = None

        async def capture_send(msg):
            nonlocal captured_prompt
            captured_prompt = msg.get("prompt", "") if isinstance(msg, dict) else msg

        mock_session.send = capture_send
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        async for _ in backend.run(
            "Hello",
            system_prompt="You are a helpful assistant.",
            session_key="s1",
        ):
            pass

        assert captured_prompt is not None
        assert "[System Instructions]" in captured_prompt
        assert "helpful assistant" in captured_prompt

    @pytest.mark.asyncio
    async def test_history_not_injected_when_empty(self):
        backend, mock_session, _ = _setup_backend_with_mock_client()
        captured_prompt = None

        async def capture_send(msg):
            nonlocal captured_prompt
            captured_prompt = msg.get("prompt", "") if isinstance(msg, dict) else msg

        mock_session.send = capture_send
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        async for _ in backend.run("Hello", session_key="s1"):
            pass

        assert captured_prompt is not None
        assert "Recent Conversation" not in captured_prompt

    @pytest.mark.asyncio
    async def test_session_reuse(self):
        """Second call with same session_key reuses the session."""
        backend, mock_session, mock_client = _setup_backend_with_mock_client()
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        # First call
        async for _ in backend.run("Hello", session_key="s1"):
            pass
        assert mock_client.create_session.call_count == 1

        # Re-wire for second call
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        # Second call — same session_key
        async for _ in backend.run("Follow up", session_key="s1"):
            pass
        # create_session should NOT be called again
        assert mock_client.create_session.call_count == 1

    @pytest.mark.asyncio
    async def test_provider_config_passed_to_session(self):
        """BYOK provider config is included in create_session opts."""
        backend, mock_session, mock_client = _setup_backend_with_mock_client()
        backend.settings = Settings(copilot_sdk_provider="openai", openai_api_key="sk-test")
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        async for _ in backend.run("Hi", session_key="byok"):
            pass

        call_arg = mock_client.create_session.call_args[0][0]
        assert "provider" in call_arg
        assert call_arg["provider"]["type"] == "openai"
        assert call_arg["provider"]["api_key"] == "sk-test"


class TestCopilotSDKToolInstructions:
    """Tests for PocketPaw tool instruction injection."""

    @pytest.mark.asyncio
    async def test_prompt_includes_tool_instructions(self):
        """Tool instructions are injected into the prompt sent to Copilot."""
        backend, mock_session, _ = _setup_backend_with_mock_client()
        captured_prompt = None

        async def capture_send(msg):
            nonlocal captured_prompt
            captured_prompt = msg.get("prompt", "") if isinstance(msg, dict) else msg

        mock_session.send = capture_send
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        with patch(
            "pocketpaw.agents.tool_bridge.get_tool_instructions_compact",
            return_value="# PocketPaw Tools\n- `web_search` — Search the web",
        ):
            async for _ in backend.run("Hello", system_prompt="Be helpful"):
                pass

        assert captured_prompt is not None
        assert "PocketPaw Tools" in captured_prompt

    @pytest.mark.asyncio
    async def test_tool_instructions_respect_policy(self):
        """When policy denies all tools, no tool section is injected."""
        backend, mock_session, _ = _setup_backend_with_mock_client()
        captured_prompt = None

        async def capture_send(msg):
            nonlocal captured_prompt
            captured_prompt = msg.get("prompt", "") if isinstance(msg, dict) else msg

        mock_session.send = capture_send
        _wire_events(mock_session, [_make_sdk_event("session.idle")])

        with patch(
            "pocketpaw.agents.tool_bridge.get_tool_instructions_compact",
            return_value="",
        ):
            async for _ in backend.run("Hello"):
                pass

        assert captured_prompt is not None
        assert "PocketPaw Tools" not in captured_prompt


class TestCopilotSDKRegistry:
    def test_registered_in_backend_registry(self):
        from pocketpaw.agents.registry import get_backend_class

        cls = get_backend_class("copilot_sdk")
        assert cls is not None
        assert cls.__name__ == "CopilotSDKBackend"

    def test_backend_info_via_registry(self):
        from pocketpaw.agents.registry import get_backend_info

        info = get_backend_info("copilot_sdk")
        assert info is not None
        assert info.name == "copilot_sdk"
        assert info.display_name == "Copilot SDK"

    def test_listed_in_backends(self):
        from pocketpaw.agents.registry import list_backends

        backends = list_backends()
        assert "copilot_sdk" in backends
