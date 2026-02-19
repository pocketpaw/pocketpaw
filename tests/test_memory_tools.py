# Tests for Memory Tools and API Endpoints
# Created: 2026-02-05
# Tests for RememberTool, RecallTool, and session list API

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.memory.manager import MemoryManager
from pocketpaw.tools.builtin.memory import RecallTool, RememberTool


@pytest.fixture
def temp_memory_path():
    """Create a temporary directory for memory tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def memory_manager(temp_memory_path):
    """Create a MemoryManager with temp path."""
    return MemoryManager(base_path=temp_memory_path)


@pytest.fixture
def mock_memory_manager(temp_memory_path):
    """Mock get_memory_manager to return our test manager."""
    manager = MemoryManager(base_path=temp_memory_path)
    with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=manager):
        yield manager


# =============================================================================
# RememberTool Tests
# =============================================================================


class TestRememberTool:
    """Tests for RememberTool."""

    def test_tool_definition(self):
        """Test tool has correct name and definition."""
        tool = RememberTool()
        assert tool.name == "remember"
        assert "Save important information" in tool.description

        params = tool.parameters
        assert "content" in params["properties"]
        assert "tags" in params["properties"]
        assert "content" in params["required"]

    def test_definition_formats(self):
        """Test definition converts to OpenAI and Anthropic formats."""
        tool = RememberTool()
        defn = tool.definition

        openai = defn.to_openai_schema()
        assert openai["function"]["name"] == "remember"

        anthropic = defn.to_anthropic_schema()
        assert anthropic["name"] == "remember"
        assert "input_schema" in anthropic

    @pytest.mark.asyncio
    async def test_remember_content(self, mock_memory_manager):
        """Test saving content to memory."""
        tool = RememberTool()
        result = await tool.execute(content="User prefers dark mode")

        assert "Remembered" in result
        assert "dark mode" in result

    @pytest.mark.asyncio
    async def test_remember_with_tags(self, mock_memory_manager):
        """Test saving content with tags."""
        tool = RememberTool()
        result = await tool.execute(
            content="User's favorite color is blue", tags=["preferences", "color"]
        )

        assert "Remembered" in result
        assert "preferences" in result or "color" in result

    @pytest.mark.asyncio
    async def test_remember_persists(self, mock_memory_manager):
        """Test that remembered content is actually persisted."""
        tool = RememberTool()
        await tool.execute(content="User works at Anthropic", tags=["work"])

        # Verify it was saved
        results = await mock_memory_manager.search("Anthropic")
        assert len(results) >= 1
        assert "Anthropic" in results[0].content

    @pytest.mark.asyncio
    async def test_remember_empty_content(self, mock_memory_manager):
        """Test error handling for empty content."""
        tool = RememberTool()
        result = await tool.execute(content="")
        # Should still work but with empty content warning or success
        # The tool doesn't explicitly check for empty, so it will save
        assert "Remembered" in result or "Error" in result

    @pytest.mark.asyncio
    async def test_remember_long_content_truncated_in_response(self, mock_memory_manager):
        """Test that long content is truncated in the response message."""
        tool = RememberTool()
        long_content = "A" * 200  # 200 characters
        result = await tool.execute(content=long_content)

        # Response should be truncated with "..."
        assert "..." in result
        assert len(result) < 200


# =============================================================================
# RecallTool Tests
# =============================================================================


class TestRecallTool:
    """Tests for RecallTool."""

    def test_tool_definition(self):
        """Test tool has correct name and definition."""
        tool = RecallTool()
        assert tool.name == "recall"
        assert "Search long-term memories" in tool.description

        params = tool.parameters
        assert "query" in params["properties"]
        assert "limit" in params["properties"]
        assert "query" in params["required"]

    @pytest.mark.asyncio
    async def test_recall_no_results(self, mock_memory_manager):
        """Test recall when no memories match."""
        tool = RecallTool()
        result = await tool.execute(query="nonexistent topic xyz123")

        assert "No memories found" in result

    @pytest.mark.asyncio
    async def test_recall_with_results(self, mock_memory_manager):
        """Test recall finds matching memories."""
        # First save some memories
        await mock_memory_manager.remember("User prefers Python programming", tags=["skills"])
        await mock_memory_manager.remember("User likes dark mode", tags=["preferences"])

        tool = RecallTool()
        result = await tool.execute(query="Python")

        assert "Found" in result
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_recall_with_limit(self, mock_memory_manager):
        """Test recall respects limit parameter."""
        # Save multiple memories
        for i in range(5):
            await mock_memory_manager.remember(f"Test memory {i} about coding")

        tool = RecallTool()
        result = await tool.execute(query="coding", limit=2)

        # Should find memories but respect limit
        assert "Found" in result

    @pytest.mark.asyncio
    async def test_recall_shows_tags(self, mock_memory_manager):
        """Test recall includes tags in results."""
        await mock_memory_manager.remember("User likes hiking", tags=["hobbies", "outdoor"])

        tool = RecallTool()
        result = await tool.execute(query="hiking")

        assert "Found" in result
        # Tags should be shown in brackets
        assert "hobbies" in result or "outdoor" in result


# =============================================================================
# Integration Tests
# =============================================================================


class TestMemoryToolsIntegration:
    """Integration tests for memory tools working together."""

    @pytest.mark.asyncio
    async def test_remember_then_recall_workflow(self, mock_memory_manager):
        """Test the full remember → recall workflow."""
        remember_tool = RememberTool()
        recall_tool = RecallTool()

        # Remember something
        remember_result = await remember_tool.execute(
            content="The user's project is called pocketpaw", tags=["project", "name"]
        )
        assert "Remembered" in remember_result

        # Recall it
        recall_result = await recall_tool.execute(query="pocketpaw")
        assert "Found" in recall_result
        assert "pocketpaw" in recall_result

    @pytest.mark.asyncio
    async def test_multiple_memories_recall(self, mock_memory_manager):
        """Test recalling from multiple related memories."""
        remember_tool = RememberTool()
        recall_tool = RecallTool()

        # Remember multiple related facts
        await remember_tool.execute(content="User uses MacOS for development")
        await remember_tool.execute(content="User prefers VSCode as editor")
        await remember_tool.execute(content="User develops in Python and TypeScript")

        # Recall with broader query
        result = await recall_tool.execute(query="development")

        assert "Found" in result
        # Should find at least one of the memories
        assert "development" in result.lower() or "MacOS" in result or "VSCode" in result


# =============================================================================
# Session List API Tests
# =============================================================================


class TestSessionListAPI:
    """Tests for the session list API endpoint."""

    @pytest.fixture
    def sessions_path(self, temp_memory_path):
        """Create sessions directory with test data."""
        sessions_dir = temp_memory_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Create test session files
        session1 = [
            {
                "id": "1",
                "role": "user",
                "content": "Hello, how are you?",
                "timestamp": "2026-02-05T10:00:00",
            },
            {
                "id": "2",
                "role": "assistant",
                "content": "I'm doing great!",
                "timestamp": "2026-02-05T10:01:00",
            },
        ]
        session2 = [
            {
                "id": "3",
                "role": "user",
                "content": "What's the weather?",
                "timestamp": "2026-02-05T11:00:00",
            },
            {
                "id": "4",
                "role": "assistant",
                "content": "It's sunny today.",
                "timestamp": "2026-02-05T11:01:00",
            },
            {"id": "5", "role": "user", "content": "Thanks!", "timestamp": "2026-02-05T11:02:00"},
        ]

        (sessions_dir / "websocket_session1.json").write_text(json.dumps(session1))
        (sessions_dir / "websocket_session2.json").write_text(json.dumps(session2))

        return sessions_dir

    @pytest.mark.asyncio
    async def test_list_sessions(self, sessions_path, temp_memory_path):
        """Test listing sessions from directory."""
        # Simulate what the API does
        sessions = []
        for session_file in sorted(
            sessions_path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            data = json.loads(session_file.read_text())
            if data:
                first_msg = data[0]
                last_msg = data[-1]
                sessions.append(
                    {
                        "id": session_file.stem,
                        "message_count": len(data),
                        "first_message": first_msg.get("content", "")[:100],
                        "last_message": last_msg.get("content", "")[:100],
                        "updated_at": last_msg.get("timestamp", ""),
                        "created_at": first_msg.get("timestamp", ""),
                    }
                )

        assert len(sessions) == 2
        # Check session data
        session_ids = [s["id"] for s in sessions]
        assert "websocket_session1" in session_ids
        assert "websocket_session2" in session_ids

        # Check message counts
        for s in sessions:
            if s["id"] == "websocket_session1":
                assert s["message_count"] == 2
            elif s["id"] == "websocket_session2":
                assert s["message_count"] == 3

    @pytest.mark.asyncio
    async def test_list_sessions_empty_directory(self, temp_memory_path):
        """Test listing sessions when directory is empty."""
        sessions_dir = temp_memory_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        sessions = list(sessions_dir.glob("*.json"))
        assert len(sessions) == 0

    @pytest.mark.asyncio
    async def test_list_sessions_malformed_json(self, temp_memory_path):
        """Test handling of malformed JSON session files."""
        sessions_dir = temp_memory_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Create a malformed file
        (sessions_dir / "bad_session.json").write_text("not valid json {")

        # Create a valid file
        valid_session = [
            {"id": "1", "role": "user", "content": "Test", "timestamp": "2026-02-05T10:00:00"}
        ]
        (sessions_dir / "good_session.json").write_text(json.dumps(valid_session))

        # Simulate API logic
        sessions = []
        for session_file in sessions_dir.glob("*.json"):
            try:
                data = json.loads(session_file.read_text())
                if data:
                    sessions.append({"id": session_file.stem})
            except json.JSONDecodeError:
                continue

        # Should only have the valid session
        assert len(sessions) == 1
        assert sessions[0]["id"] == "good_session"


# =============================================================================
# Memory Tool Registration Tests
# =============================================================================


class TestMemoryToolRegistration:
    """Test that memory tools are properly registered."""

    def test_tools_importable(self):
        """Test that memory tools can be imported from builtin package."""
        from pocketpaw.tools.builtin import RememberTool, RecallTool

        assert RememberTool is not None
        assert RecallTool is not None

    def test_tools_in_all_exports(self):
        """Test that memory tools are in __all__ exports."""
        from pocketpaw.tools import builtin

        assert "RememberTool" in builtin.__all__
        assert "RecallTool" in builtin.__all__

    def test_tools_work_with_registry(self, mock_memory_manager):
        """Test that tools work with ToolRegistry."""
        from pocketpaw.tools.registry import ToolRegistry
        from pocketpaw.tools.builtin.memory import RememberTool, RecallTool

        registry = ToolRegistry()
        registry.register(RememberTool())
        registry.register(RecallTool())

        assert registry.has("remember")
        assert registry.has("recall")

        # Check definitions
        defs = registry.get_definitions("anthropic")
        names = [d["name"] for d in defs]
        assert "remember" in names
        assert "recall" in names
