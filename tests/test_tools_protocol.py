# Tests for Tool System
# Created: 2026-02-02


import pytest
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from pocketclaw.tools.protocol import ToolProtocol, BaseTool, ToolDefinition
from pocketclaw.tools.registry import ToolRegistry
from pocketclaw.tools.builtin.shell import ShellTool
from pocketclaw.tools.builtin.filesystem import ReadFileTool, WriteFileTool, ListDirTool
from pocketclaw.config import Settings


class MockTool(BaseTool):
    """Mock tool for testing registry."""

    @property
    def name(self) -> str:
        return "mock_tool"

    @property
    def description(self) -> str:
        return "A mock tool."

    async def execute(self, param: str) -> str:
        return f"Executed with {param}"


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_register_and_get(self):
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)

        assert registry.has("mock_tool")
        assert registry.get("mock_tool") == tool
        assert "mock_tool" in registry.tool_names

    def test_unregister(self):
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)
        registry.unregister("mock_tool")

        assert not registry.has("mock_tool")

    def test_get_definitions(self):
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)

        defs = registry.get_definitions("openai")
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "mock_tool"

    @pytest.mark.asyncio
    async def test_execute(self):
        registry = ToolRegistry()
        tool = MockTool()
        registry.register(tool)

        result = await registry.execute("mock_tool", param="test")
        assert result == "Executed with test"

    @pytest.mark.asyncio
    async def test_execute_missing(self):
        registry = ToolRegistry()
        result = await registry.execute("missing_tool")
        assert "Error: Tool 'missing_tool' not found" in result


class TestShellTool:
    """Tests for ShellTool."""

    @pytest.mark.asyncio
    async def test_execute_simple(self):
        tool = ShellTool()
        result = (
            await tool.execute_command("echo 'hello'")
            if hasattr(tool, "execute_command")
            else await tool.execute(command="echo 'hello'")
        )
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_security_check(self):
        tool = ShellTool()
        result = await tool.execute(command="rm -rf /")
        assert "Dangerous command blocked" in result

    @pytest.mark.asyncio
    async def test_timeout(self):
        # Create tool with short timeout
        tool = ShellTool(timeout=1)
        # Sleep for 2 seconds
        result = await tool.execute(command="sleep 2")
        assert "Command timed out" in result


@pytest.fixture
def temp_jail():
    """Create a temporary directory for file jail."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_settings(temp_jail):
    """Mock settings with custom file jail."""
    settings = Settings(file_jail_path=temp_jail)
    with (
        patch("pocketclaw.tools.builtin.filesystem.get_settings", return_value=settings),
        patch("pocketclaw.tools.builtin.shell.get_settings", return_value=settings),
    ):
        yield settings


class TestFilesystemTools:
    """Tests for filesystem tools."""

    @pytest.mark.asyncio
    async def test_write_and_read(self, temp_jail, mock_settings):
        write_tool = WriteFileTool()
        read_tool = ReadFileTool()

        # Write
        file_path = str(temp_jail / "test.txt")
        result = await write_tool.execute(path=file_path, content="Hello World")
        assert "Successfully wrote" in result

        # Read
        content = await read_tool.execute(path=file_path)
        assert content == "Hello World"

    @pytest.mark.asyncio
    async def test_jail_break_attempt(self, temp_jail, mock_settings):
        read_tool = ReadFileTool()

        # Try to read outside jail
        outside = str(temp_jail.parent / "secret.txt")
        result = await read_tool.execute(path=outside)
        if "Access denied" not in result:
            # Depending on how resolve works, it might be same dir if parent is tmp
            # Let's try explicit relative path
            outside = str(temp_jail / "../secret.txt")
            result = await read_tool.execute(path=outside)

        # It's possible for temp dir to be weird, but let's check basic protection
        # If result is "File not found" it might mean it resolved but didn't error on jail
        # We want explicit jail error
        assert "Access denied" in result

    @pytest.mark.asyncio
    async def test_list_dir(self, temp_jail, mock_settings):
        list_tool = ListDirTool()
        write_tool = WriteFileTool()

        # Create some files
        await write_tool.execute(path=str(temp_jail / "a.txt"), content="a")
        await write_tool.execute(path=str(temp_jail / "b.txt"), content="b")

        # List
        result = await list_tool.execute(path=str(temp_jail))
        assert "a.txt" in result
        assert "b.txt" in result
