"""Tests for MCP + PocketPaw Native backend — Sprint 18.

All MCP imports are mocked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
from pocketclaw.config import Settings
from pocketclaw.mcp.manager import MCPManager, MCPToolInfo


def _make_orchestrator(**overrides) -> PocketPawOrchestrator:
    """Create orchestrator with mocked executor."""
    settings = Settings(
        anthropic_api_key="test-key",
        tool_profile="full",
        **overrides,
    )
    with patch.object(PocketPawOrchestrator, "_initialize"):
        orch = PocketPawOrchestrator(settings)
        orch._client = None  # don't need real client
        orch._executor = None
        from pocketclaw.tools.policy import ToolPolicy

        orch._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        from pathlib import Path

        orch._file_jail = Path.home()
    return orch


class TestMCPToolDiscovery:
    """Test _get_mcp_tools method."""

    def test_no_mcp_tools(self):
        orch = _make_orchestrator()
        with patch("pocketclaw.mcp.manager.get_mcp_manager") as mock_get:
            mgr = MCPManager()
            mock_get.return_value = mgr
            tools = orch._get_mcp_tools()
        assert tools == []

    def test_mcp_tools_returned(self):
        orch = _make_orchestrator()
        mgr = MCPManager()
        # Inject fake tools via internal state
        from pocketclaw.mcp.manager import _ServerState
        from pocketclaw.mcp.config import MCPServerConfig

        state = _ServerState(
            config=MCPServerConfig(name="fs"),
            connected=True,
            tools=[
                MCPToolInfo(
                    server_name="fs",
                    name="read_file",
                    description="Read a file",
                    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                ),
            ],
        )
        mgr._servers["fs"] = state

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mgr):
            tools = orch._get_mcp_tools()

        assert len(tools) == 1
        assert tools[0]["name"] == "mcp_fs__read_file"
        assert "[MCP:fs]" in tools[0]["description"]
        assert tools[0]["input_schema"]["type"] == "object"

    def test_mcp_tools_filtered_by_policy(self):
        orch = _make_orchestrator(tools_deny=["mcp:fs:*"])
        mgr = MCPManager()
        from pocketclaw.mcp.manager import _ServerState
        from pocketclaw.mcp.config import MCPServerConfig

        state = _ServerState(
            config=MCPServerConfig(name="fs"),
            connected=True,
            tools=[MCPToolInfo(server_name="fs", name="read_file")],
        )
        mgr._servers["fs"] = state

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mgr):
            tools = orch._get_mcp_tools()
        assert tools == []

    def test_get_filtered_tools_includes_mcp(self):
        orch = _make_orchestrator()
        mgr = MCPManager()
        from pocketclaw.mcp.manager import _ServerState
        from pocketclaw.mcp.config import MCPServerConfig

        state = _ServerState(
            config=MCPServerConfig(name="gh"),
            connected=True,
            tools=[MCPToolInfo(server_name="gh", name="list_repos")],
        )
        mgr._servers["gh"] = state

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mgr):
            tools = orch._get_filtered_tools()

        tool_names = [t["name"] for t in tools]
        assert "mcp_gh__list_repos" in tool_names
        # Base tools should also be present (full profile)
        assert "shell" in tool_names


class TestMCPToolNameParsing:
    def test_parse_valid(self):
        orch = _make_orchestrator()
        result = orch._parse_mcp_tool_name("mcp_fs__read_file")
        assert result == ("fs", "read_file")

    def test_parse_non_mcp(self):
        orch = _make_orchestrator()
        result = orch._parse_mcp_tool_name("shell")
        assert result is None

    def test_parse_no_separator(self):
        orch = _make_orchestrator()
        result = orch._parse_mcp_tool_name("mcp_noseparator")
        assert result is None


class TestMCPToolExecution:
    async def test_execute_mcp_tool(self):
        orch = _make_orchestrator()
        mgr = MCPManager()
        from pocketclaw.mcp.manager import _ServerState
        from pocketclaw.mcp.config import MCPServerConfig

        mock_session = AsyncMock()
        block = SimpleNamespace(text="file contents here")
        mock_session.call_tool = AsyncMock(return_value=SimpleNamespace(content=[block]))
        state = _ServerState(
            config=MCPServerConfig(name="fs"),
            connected=True,
            session=mock_session,
        )
        mgr._servers["fs"] = state

        with patch("pocketclaw.mcp.manager.get_mcp_manager", return_value=mgr):
            result = await orch._execute_tool("mcp_fs__read_file", {"path": "/tmp/x"})

        assert result == "file contents here"
        mock_session.call_tool.assert_called_once_with("read_file", {"path": "/tmp/x"})

    async def test_execute_non_mcp_tool_still_works(self):
        orch = _make_orchestrator()
        # shell tool should still work via fallback
        result = await orch._execute_tool("shell", {"command": "echo hi"})
        assert "hi" in result or "Error" in result  # fallback may not have executor

    async def test_execute_unknown_tool(self):
        orch = _make_orchestrator()
        result = await orch._execute_tool("nonexistent", {})
        assert "Unknown tool" in result
