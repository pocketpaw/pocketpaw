"""Tests for agents/tool_bridge.py — tool adaptation for multi-backend support."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.config import Settings


class TestInstantiateAllTools:
    def test_returns_list_of_tools(self):
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_excludes_shell_and_filesystem(self):
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools()
        names = {type(t).__name__ for t in tools}
        assert "ShellTool" not in names
        assert "ReadFileTool" not in names
        assert "WriteFileTool" not in names
        assert "ListDirTool" not in names

    def test_excludes_browser_and_desktop(self):
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools()
        names = {type(t).__name__ for t in tools}
        assert "BrowserTool" not in names
        assert "DesktopTool" not in names

    def test_handles_import_errors_gracefully(self):
        """If a tool module fails to import, it's skipped without crashing."""
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        # Patch one module to raise ImportError
        with patch("importlib.import_module") as mock_import:
            mock_import.side_effect = ImportError("test failure")
            tools = _instantiate_all_tools()
            # Should return empty list (all tools failed to import)
            assert tools == []

    def test_all_tools_have_name_property(self):
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools()
        for tool in tools:
            assert hasattr(tool, "name")
            assert isinstance(tool.name, str)
            assert len(tool.name) > 0


class TestBuildOpenAIFunctionTools:
    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_returns_function_tools(self, mock_instantiate):
        """When SDK is available, returns FunctionTool list."""
        mock_tool = MagicMock()
        mock_tool.name = "web_search"
        mock_tool.definition.name = "web_search"
        mock_tool.definition.description = "Search the web"
        mock_tool.definition.parameters = {"type": "object", "properties": {}}
        mock_instantiate.return_value = [mock_tool]

        # Mock the agents module
        mock_ft_cls = MagicMock()
        mock_ft_instance = MagicMock()
        mock_ft_cls.return_value = mock_ft_instance

        with patch.dict("sys.modules", {"agents": MagicMock(FunctionTool=mock_ft_cls)}):
            from pocketpaw.agents import tool_bridge

            # Force reimport of FunctionTool inside function
            result = tool_bridge.build_openai_function_tools(Settings())
            assert len(result) > 0

    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_returns_empty_without_sdk(self, mock_instantiate):
        """Returns empty list when OpenAI Agents SDK is not installed."""
        mock_instantiate.return_value = []

        # Ensure agents module is not importable
        with patch.dict("sys.modules", {"agents": None}):
            from pocketpaw.agents.tool_bridge import build_openai_function_tools

            result = build_openai_function_tools(Settings())
            assert result == []

    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_policy_filtering_deny(self, mock_instantiate):
        """Denied tools are excluded from the result."""
        mock_tool = MagicMock()
        mock_tool.name = "web_search"
        mock_tool.definition.name = "web_search"
        mock_tool.definition.description = "Search the web"
        mock_tool.definition.parameters = {"type": "object", "properties": {}}
        mock_instantiate.return_value = [mock_tool]

        settings = Settings(tools_deny=["web_search"])

        mock_ft_cls = MagicMock()
        with patch.dict("sys.modules", {"agents": MagicMock(FunctionTool=mock_ft_cls)}):
            from pocketpaw.agents.tool_bridge import build_openai_function_tools

            result = build_openai_function_tools(settings)
            # web_search should be denied
            assert len(result) == 0

    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_policy_filtering_minimal_profile(self, mock_instantiate):
        """Minimal profile only includes memory/session tools."""
        tools = []
        for name in ["remember", "recall", "web_search", "gmail_search"]:
            t = MagicMock()
            t.name = name
            t.definition.name = name
            t.definition.description = f"Tool: {name}"
            t.definition.parameters = {"type": "object", "properties": {}}
            tools.append(t)
        mock_instantiate.return_value = tools

        settings = Settings(tool_profile="minimal")

        mock_ft_cls = MagicMock()
        with patch.dict("sys.modules", {"agents": MagicMock(FunctionTool=mock_ft_cls)}):
            from pocketpaw.agents.tool_bridge import build_openai_function_tools

            result = build_openai_function_tools(settings)
            # Only remember and recall should pass minimal profile
            assert len(result) == 2


class TestMakeInvokeCallback:
    @pytest.mark.asyncio
    async def test_callback_parses_json_and_calls_execute(self):
        from pocketpaw.agents.tool_bridge import _make_invoke_callback

        mock_tool = MagicMock()
        mock_tool.name = "web_search"
        mock_tool.execute = AsyncMock(return_value="search results")

        callback = _make_invoke_callback(mock_tool)
        result = await callback(None, json.dumps({"query": "test"}))

        mock_tool.execute.assert_called_once_with(query="test")
        assert result == "search results"

    @pytest.mark.asyncio
    async def test_callback_returns_error_for_invalid_json(self):
        from pocketpaw.agents.tool_bridge import _make_invoke_callback

        mock_tool = MagicMock()
        mock_tool.name = "web_search"

        callback = _make_invoke_callback(mock_tool)
        result = await callback(None, "not json{{{")

        assert "Error" in result
        assert "invalid JSON" in result

    @pytest.mark.asyncio
    async def test_callback_catches_execution_exceptions(self):
        from pocketpaw.agents.tool_bridge import _make_invoke_callback

        mock_tool = MagicMock()
        mock_tool.name = "web_search"
        mock_tool.execute = AsyncMock(side_effect=RuntimeError("API down"))

        callback = _make_invoke_callback(mock_tool)
        result = await callback(None, '{"query": "test"}')

        assert "Error" in result
        assert "API down" in result

    @pytest.mark.asyncio
    async def test_callback_handles_empty_args(self):
        from pocketpaw.agents.tool_bridge import _make_invoke_callback

        mock_tool = MagicMock()
        mock_tool.name = "recall"
        mock_tool.execute = AsyncMock(return_value="all memories")

        callback = _make_invoke_callback(mock_tool)
        result = await callback(None, "")

        mock_tool.execute.assert_called_once_with()
        assert result == "all memories"


class TestGetToolInstructionsCompact:
    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_returns_markdown(self, mock_instantiate):
        mock_tool = MagicMock()
        mock_tool.name = "web_search"
        mock_tool.definition.name = "web_search"
        mock_tool.definition.description = "Search the web. Returns results."
        mock_tool.definition.parameters = {"type": "object", "properties": {}}
        mock_instantiate.return_value = [mock_tool]

        from pocketpaw.agents.tool_bridge import get_tool_instructions_compact

        result = get_tool_instructions_compact(Settings())
        assert "# PocketPaw Tools" in result
        assert "`web_search`" in result
        assert "python -m pocketpaw.tools.cli" in result

    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_respects_policy_filtering(self, mock_instantiate):
        tools = []
        for name in ["web_search", "gmail_search"]:
            t = MagicMock()
            t.name = name
            t.definition.name = name
            t.definition.description = f"Tool: {name}"
            t.definition.parameters = {"type": "object", "properties": {}}
            tools.append(t)
        mock_instantiate.return_value = tools

        from pocketpaw.agents.tool_bridge import get_tool_instructions_compact

        settings = Settings(tools_deny=["gmail_search"])
        result = get_tool_instructions_compact(settings)

        assert "`web_search`" in result
        assert "gmail_search" not in result

    @patch("pocketpaw.agents.tool_bridge._instantiate_all_tools")
    def test_returns_empty_when_no_tools(self, mock_instantiate):
        mock_instantiate.return_value = []

        from pocketpaw.agents.tool_bridge import get_tool_instructions_compact

        result = get_tool_instructions_compact(Settings())
        assert result == ""
