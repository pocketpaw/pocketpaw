"""Tests for Google ADK backend — mocked (no real SDK needed)."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings

# --- Mock google.genai.types for all run() tests ---
_mock_genai_types = MagicMock()
_mock_genai = MagicMock()
_mock_genai.types = _mock_genai_types


class TestGoogleADKInfo:
    def test_info_metadata(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        info = GoogleADKBackend.info()
        assert info.name == "google_adk"
        assert info.display_name == "Google ADK"
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MCP in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities
        assert "google_search" in info.builtin_tools
        assert "code_execution" in info.builtin_tools

    def test_tool_policy_map(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        info = GoogleADKBackend.info()
        assert info.tool_policy_map["google_search"] == "browser"
        assert info.tool_policy_map["code_execution"] == "shell"

    def test_install_hint(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        info = GoogleADKBackend.info()
        assert info.install_hint["pip_package"] == "google-adk"
        assert info.install_hint["pip_spec"] == "pocketpaw[google-adk]"
        assert info.install_hint["verify_import"] == "google.adk"
        assert "google_api_key" in info.required_keys
        assert "google" in info.supported_providers


class TestGoogleADKInit:
    @patch.dict(sys.modules, {"google.adk": MagicMock()})
    def test_init_with_sdk(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        backend = GoogleADKBackend(Settings())
        assert backend._sdk_available is True

    def test_init_without_sdk(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        backend = GoogleADKBackend.__new__(GoogleADKBackend)
        backend.settings = Settings()
        backend._sdk_available = False
        backend._stop_flag = False
        backend._runner = None
        backend._sessions = {}
        backend._custom_tools = None
        assert backend._sdk_available is False

    @pytest.mark.asyncio
    async def test_run_without_sdk_yields_error(self):
        from pocketpaw.agents.google_adk import GoogleADKBackend

        backend = GoogleADKBackend.__new__(GoogleADKBackend)
        backend.settings = Settings()
        backend._sdk_available = False
        backend._stop_flag = False
        backend._runner = None
        backend._sessions = {}
        backend._custom_tools = None

        events = []
        async for event in backend.run("test"):
            events.append(event)

        assert any(e.type == "error" for e in events)
        assert any("not installed" in e.content for e in events if e.type == "error")


# --- Mock ADK event helpers ---


def _make_text_event(text: str, author: str = "PocketPaw"):
    """Create a mock ADK event with text content."""
    part = SimpleNamespace(text=text, function_call=None, function_response=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(author=author, content=content)


def _make_function_call_event(name: str, args: dict | None = None):
    """Create a mock ADK event with a function call."""
    fc = SimpleNamespace(name=name, args=args or {})
    part = SimpleNamespace(text=None, function_call=fc, function_response=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(author="PocketPaw", content=content)


def _make_function_response_event(name: str, response: dict | None = None):
    """Create a mock ADK event with a function response."""
    fr = SimpleNamespace(name=name, response=response or {})
    part = SimpleNamespace(text=None, function_call=None, function_response=fr)
    content = SimpleNamespace(role="tool", parts=[part])
    return SimpleNamespace(author="tool", content=content)


def _make_mock_runner(events):
    """Create a mock runner that yields given events."""

    async def mock_run_async(**kwargs):
        for ev in events:
            yield ev

    mock_session_service = AsyncMock()
    mock_session_service.create_session = AsyncMock()
    mock_runner = MagicMock()
    mock_runner.run_async = mock_run_async
    mock_runner.session_service = mock_session_service
    return mock_runner


def _make_backend():
    """Create a backend instance with mocked SDK availability."""
    from pocketpaw.agents.google_adk import GoogleADKBackend

    backend = GoogleADKBackend.__new__(GoogleADKBackend)
    backend.settings = Settings()
    backend._sdk_available = True
    backend._stop_flag = False
    backend._runner = None
    backend._sessions = {}
    backend._custom_tools = []
    return backend


def _run_patches(backend, mock_runner):
    """Return a combined context manager for common patches."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        with (
            patch.object(backend, "_build_custom_tools", return_value=[]),
            patch.object(backend, "_build_mcp_toolsets", return_value=[]),
            patch.object(backend, "_get_runner", return_value=mock_runner),
            patch.dict(sys.modules, {"google.genai": _mock_genai}),
        ):
            yield

    return _ctx()


class TestGoogleADKRun:
    @pytest.mark.asyncio
    async def test_text_events(self):
        backend = _make_backend()
        mock_runner = _make_mock_runner(
            [
                _make_text_event("Hello "),
                _make_text_event("world!"),
            ]
        )

        with _run_patches(backend, mock_runner):
            events = []
            async for event in backend.run("Hi"):
                events.append(event)

        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 2
        assert messages[0].content == "Hello "
        assert messages[1].content == "world!"
        assert any(e.type == "done" for e in events)

    @pytest.mark.asyncio
    async def test_function_call_events(self):
        backend = _make_backend()
        mock_runner = _make_mock_runner(
            [
                _make_function_call_event("google_search", {"query": "test"}),
            ]
        )

        with _run_patches(backend, mock_runner):
            events = []
            async for event in backend.run("search test"):
                events.append(event)

        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].metadata["name"] == "google_search"
        assert tool_events[0].metadata["input"] == {"query": "test"}

    @pytest.mark.asyncio
    async def test_function_response_events(self):
        backend = _make_backend()
        mock_runner = _make_mock_runner(
            [
                _make_function_response_event("google_search", {"results": "found"}),
            ]
        )

        with _run_patches(backend, mock_runner):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        results = [e for e in events if e.type == "tool_result"]
        assert len(results) == 1
        assert results[0].metadata["name"] == "google_search"

    @pytest.mark.asyncio
    async def test_handles_errors(self):
        backend = _make_backend()

        async def mock_run_async(**kwargs):
            raise RuntimeError("ADK SDK error")
            yield  # noqa: F841 — makes this an async generator

        mock_session_service = AsyncMock()
        mock_session_service.create_session = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner.session_service = mock_session_service

        with _run_patches(backend, mock_runner):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert len(errors) >= 1
        assert any("ADK" in e.content for e in errors)

    @pytest.mark.asyncio
    async def test_stop_flag(self):
        backend = _make_backend()
        mock_runner = _make_mock_runner(
            [
                _make_text_event("First"),
                _make_text_event("Second"),
                _make_text_event("Third"),
            ]
        )

        with _run_patches(backend, mock_runner):
            events = []
            async for event in backend.run("test"):
                events.append(event)
                if event.type == "message":
                    backend._stop_flag = True
                    break

        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_max_turns_limit(self):
        backend = _make_backend()
        backend.settings = Settings(google_adk_max_turns=2)

        mock_runner = _make_mock_runner(
            [
                _make_function_call_event("tool1"),
                _make_function_call_event("tool2"),
                _make_function_call_event("tool3"),
            ]
        )

        with _run_patches(backend, mock_runner):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert any("Max turns" in e.content for e in errors)


class TestGoogleADKSessions:
    @pytest.mark.asyncio
    async def test_new_session_created(self):
        backend = _make_backend()

        async def mock_run_async(**kwargs):
            return
            yield  # noqa: F841

        mock_session_service = AsyncMock()
        mock_session_service.create_session = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner.session_service = mock_session_service

        with _run_patches(backend, mock_runner):
            async for _ in backend.run("Hi", session_key="s1"):
                pass

        assert "s1" in backend._sessions
        mock_session_service.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_reused(self):
        backend = _make_backend()
        backend._sessions["s1"] = "existing-session-id"

        async def mock_run_async(**kwargs):
            return
            yield  # noqa

        mock_session_service = AsyncMock()
        mock_session_service.create_session = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.run_async = mock_run_async
        mock_runner.session_service = mock_session_service

        with _run_patches(backend, mock_runner):
            async for _ in backend.run("Follow up", session_key="s1"):
                pass

        assert backend._sessions["s1"] == "existing-session-id"

    @pytest.mark.asyncio
    async def test_history_seeded_on_new_session(self):
        backend = _make_backend()
        captured_instruction = None

        def capture_runner(instruction, tools):
            nonlocal captured_instruction
            captured_instruction = instruction

            async def mock_run_async(**kwargs):
                return
                yield  # noqa

            mock_session_service = AsyncMock()
            mock_session_service.create_session = AsyncMock()
            mock_runner = MagicMock()
            mock_runner.run_async = mock_run_async
            mock_runner.session_service = mock_session_service
            return mock_runner

        history = [
            {"role": "user", "content": "From previous backend"},
            {"role": "assistant", "content": "I remember that"},
        ]

        with (
            patch.object(backend, "_build_custom_tools", return_value=[]),
            patch.object(backend, "_build_mcp_toolsets", return_value=[]),
            patch.object(backend, "_get_runner", side_effect=capture_runner),
            patch.dict(sys.modules, {"google.genai": _mock_genai}),
        ):
            async for _ in backend.run(
                "Continue",
                system_prompt="You are PocketPaw.",
                history=history,
                session_key="s1",
            ):
                pass

        assert captured_instruction is not None
        assert "Recent Conversation" in captured_instruction
        assert "From previous backend" in captured_instruction


class TestGoogleADKMCP:
    def test_build_mcp_toolsets_no_deps(self):
        backend = _make_backend()

        # Should return empty list when MCP deps not available
        with patch("builtins.__import__", side_effect=ImportError):
            result = backend._build_mcp_toolsets()
        assert result == []

    def test_build_mcp_toolsets_no_config(self):
        backend = _make_backend()

        with patch(
            "pocketpaw.agents.google_adk.GoogleADKBackend._build_mcp_toolsets",
            return_value=[],
        ):
            result = backend._build_mcp_toolsets()
        assert result == []

    def test_build_mcp_toolsets_policy_blocks_server(self):
        """MCP servers denied by tool policy should be excluded."""
        backend = _make_backend()
        backend.settings.tools_deny = ["mcp:blocked_server:*"]

        mock_cfg_blocked = MagicMock()
        mock_cfg_blocked.name = "blocked_server"
        mock_cfg_blocked.transport = "stdio"
        mock_cfg_blocked.command = "echo"
        mock_cfg_blocked.args = []
        mock_cfg_blocked.env = {}

        mock_cfg_allowed = MagicMock()
        mock_cfg_allowed.name = "allowed_server"
        mock_cfg_allowed.transport = "stdio"
        mock_cfg_allowed.command = "echo"
        mock_cfg_allowed.args = []
        mock_cfg_allowed.env = {}

        mock_toolset_cls = MagicMock()
        mock_mcp_tool = MagicMock()
        mock_mcp_tool.McpToolset = mock_toolset_cls
        mock_session_mgr = MagicMock()
        mock_mcp = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "google.adk.tools.mcp_tool": mock_mcp_tool,
                "google.adk.tools.mcp_tool.mcp_session_manager": mock_session_mgr,
                "mcp": mock_mcp,
            },
        ), patch(
            "pocketpaw.mcp.config.load_mcp_config",
            return_value=[mock_cfg_blocked, mock_cfg_allowed],
        ):
            result = backend._build_mcp_toolsets()

        # Only the allowed server should produce a toolset
        assert len(result) == 1
        assert mock_toolset_cls.call_count == 1

    def test_build_mcp_toolsets_policy_blocks_group_mcp(self):
        """Denying group:mcp should block all MCP servers."""
        backend = _make_backend()
        backend.settings.tools_deny = ["group:mcp"]

        mock_cfg = MagicMock()
        mock_cfg.name = "any_server"
        mock_cfg.transport = "stdio"
        mock_cfg.command = "echo"
        mock_cfg.args = []
        mock_cfg.env = {}

        mock_mcp_tool = MagicMock()
        mock_session_mgr = MagicMock()
        mock_mcp = MagicMock()

        with patch.dict(
            sys.modules,
            {
                "google.adk.tools.mcp_tool": mock_mcp_tool,
                "google.adk.tools.mcp_tool.mcp_session_manager": mock_session_mgr,
                "mcp": mock_mcp,
            },
        ), patch(
            "pocketpaw.mcp.config.load_mcp_config",
            return_value=[mock_cfg],
        ):
            result = backend._build_mcp_toolsets()

        assert result == []


class TestGoogleADKStatus:
    @pytest.mark.asyncio
    async def test_status_dict(self):
        backend = _make_backend()
        backend._sessions = {"s1": "id1"}

        status = await backend.get_status()
        assert status["backend"] == "google_adk"
        assert status["available"] is True
        assert status["active_sessions"] == 1
        assert "model" in status

    @pytest.mark.asyncio
    async def test_stop_sets_flag(self):
        backend = _make_backend()

        await backend.stop()
        assert backend._stop_flag is True


class TestGoogleADKBackwardCompat:
    def test_gemini_cli_alias(self):
        from pocketpaw.agents.google_adk import GeminiCLIBackend, GoogleADKBackend

        assert GeminiCLIBackend is GoogleADKBackend
