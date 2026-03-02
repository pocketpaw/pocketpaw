# Tests for paw module soul tools.
# Created: 2026-03-02
# Covers: SoulRememberTool, SoulRecallTool, SoulEditCoreTool, SoulStatusTool —
#         execute() behaviour, tool names, and JSON Schema correctness.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketpaw.paw.tools import (
    SoulEditCoreTool,
    SoulRecallTool,
    SoulRememberTool,
    SoulStatusTool,
)


# ---------------------------------------------------------------------------
# Shared soul fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_soul():
    soul = MagicMock()
    soul.name = "TestSoul"
    soul.to_system_prompt.return_value = "I am TestSoul."
    soul.state = MagicMock(mood="curious", energy=85, social_battery=90)
    soul.self_model = None
    soul.remember = AsyncMock(return_value="mem_123")
    soul.recall = AsyncMock(
        return_value=[MagicMock(content="project uses Python", importance=8, emotion=None)]
    )
    soul.observe = AsyncMock()
    soul.edit_core_memory = AsyncMock()
    soul.save = AsyncMock()
    soul.export = AsyncMock()
    return soul


# ---------------------------------------------------------------------------
# SoulRememberTool
# ---------------------------------------------------------------------------


class TestSoulRememberTool:
    def test_name(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        assert tool.name == "soul_remember"

    def test_parameters_schema_has_content_required(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        params = tool.parameters

        assert params["type"] == "object"
        assert "content" in params["properties"]
        assert "content" in params["required"]

    def test_parameters_schema_importance_is_integer(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        params = tool.parameters

        assert params["properties"]["importance"]["type"] == "integer"
        assert params["properties"]["importance"]["minimum"] == 1
        assert params["properties"]["importance"]["maximum"] == 10

    @pytest.mark.asyncio
    async def test_execute_stores_memory(self, mock_soul):
        tool = SoulRememberTool(mock_soul)

        result = await tool.execute(content="The project uses FastAPI", importance=7)

        mock_soul.remember.assert_awaited_once_with(
            "The project uses FastAPI", importance=7
        )
        assert "Remembered" in result

    @pytest.mark.asyncio
    async def test_execute_default_importance_is_5(self, mock_soul):
        tool = SoulRememberTool(mock_soul)

        await tool.execute(content="Some fact")

        mock_soul.remember.assert_awaited_once_with("Some fact", importance=5)

    @pytest.mark.asyncio
    async def test_execute_truncates_long_content_in_result(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        long_content = "x" * 200

        result = await tool.execute(content=long_content)

        assert "..." in result

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_soul_failure(self, mock_soul):
        mock_soul.remember = AsyncMock(side_effect=RuntimeError("disk full"))
        tool = SoulRememberTool(mock_soul)

        result = await tool.execute(content="Fact")

        assert "Error:" in result
        assert "disk full" in result


# ---------------------------------------------------------------------------
# SoulRecallTool
# ---------------------------------------------------------------------------


class TestSoulRecallTool:
    def test_name(self, mock_soul):
        tool = SoulRecallTool(mock_soul)
        assert tool.name == "soul_recall"

    def test_parameters_schema_has_query_required(self, mock_soul):
        tool = SoulRecallTool(mock_soul)
        params = tool.parameters

        assert "query" in params["properties"]
        assert "query" in params["required"]

    @pytest.mark.asyncio
    async def test_execute_returns_formatted_results(self, mock_soul):
        tool = SoulRecallTool(mock_soul)

        result = await tool.execute(query="Python")

        assert "project uses Python" in result
        mock_soul.recall.assert_awaited_once_with("Python", limit=5)

    @pytest.mark.asyncio
    async def test_execute_no_memories_returns_not_found(self, mock_soul):
        mock_soul.recall = AsyncMock(return_value=[])
        tool = SoulRecallTool(mock_soul)

        result = await tool.execute(query="nonexistent topic")

        assert "No memories found" in result

    @pytest.mark.asyncio
    async def test_execute_respects_limit_param(self, mock_soul):
        tool = SoulRecallTool(mock_soul)

        await tool.execute(query="test", limit=3)

        mock_soul.recall.assert_awaited_once_with("test", limit=3)

    @pytest.mark.asyncio
    async def test_execute_shows_memory_importance(self, mock_soul):
        tool = SoulRecallTool(mock_soul)

        result = await tool.execute(query="Python")

        # importance=8 should appear in the formatted output
        assert "8" in result

    @pytest.mark.asyncio
    async def test_execute_includes_emotion_when_present(self, mock_soul):
        mem = MagicMock(content="exciting discovery", importance=7, emotion="joy")
        mock_soul.recall = AsyncMock(return_value=[mem])
        tool = SoulRecallTool(mock_soul)

        result = await tool.execute(query="discovery")

        assert "joy" in result

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_soul_failure(self, mock_soul):
        mock_soul.recall = AsyncMock(side_effect=RuntimeError("connection lost"))
        tool = SoulRecallTool(mock_soul)

        result = await tool.execute(query="test")

        assert "Error:" in result


# ---------------------------------------------------------------------------
# SoulEditCoreTool
# ---------------------------------------------------------------------------


class TestSoulEditCoreTool:
    def test_name(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)
        assert tool.name == "soul_edit_core"

    def test_parameters_schema_has_persona_and_human(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)
        params = tool.parameters

        assert "persona" in params["properties"]
        assert "human" in params["properties"]
        # Both are optional — required list should be empty
        assert params["required"] == []

    @pytest.mark.asyncio
    async def test_execute_with_persona(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)

        result = await tool.execute(persona="I am a code assistant.")

        mock_soul.edit_core_memory.assert_awaited_once_with(persona="I am a code assistant.")
        assert "persona" in result

    @pytest.mark.asyncio
    async def test_execute_with_human(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)

        result = await tool.execute(human="Alice, a senior engineer.")

        mock_soul.edit_core_memory.assert_awaited_once_with(human="Alice, a senior engineer.")
        assert "human" in result

    @pytest.mark.asyncio
    async def test_execute_with_both(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)

        result = await tool.execute(
            persona="Code assistant.", human="Bob, a data scientist."
        )

        mock_soul.edit_core_memory.assert_awaited_once_with(
            persona="Code assistant.", human="Bob, a data scientist."
        )
        assert "persona" in result or "human" in result

    @pytest.mark.asyncio
    async def test_execute_with_neither_returns_error(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)

        result = await tool.execute()

        assert "Error:" in result
        mock_soul.edit_core_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_soul_failure(self, mock_soul):
        mock_soul.edit_core_memory = AsyncMock(side_effect=RuntimeError("bad state"))
        tool = SoulEditCoreTool(mock_soul)

        result = await tool.execute(persona="New persona")

        assert "Error:" in result


# ---------------------------------------------------------------------------
# SoulStatusTool
# ---------------------------------------------------------------------------


class TestSoulStatusTool:
    def test_name(self, mock_soul):
        tool = SoulStatusTool(mock_soul)
        assert tool.name == "soul_status"

    def test_parameters_schema_empty(self, mock_soul):
        tool = SoulStatusTool(mock_soul)
        params = tool.parameters

        assert params["type"] == "object"
        assert params["properties"] == {}
        assert params["required"] == []

    @pytest.mark.asyncio
    async def test_execute_returns_json_with_mood_energy(self, mock_soul):
        tool = SoulStatusTool(mock_soul)

        result = await tool.execute()

        data = json.loads(result)
        assert data["mood"] == "curious"
        assert data["energy"] == 85
        assert data["social_battery"] == 90

    @pytest.mark.asyncio
    async def test_execute_includes_domains_when_self_model_present(self, mock_soul):
        img = MagicMock(domain="Python", confidence=0.9)
        self_model = MagicMock()
        self_model.get_active_self_images.return_value = [img]
        mock_soul.self_model = self_model
        tool = SoulStatusTool(mock_soul)

        result = await tool.execute()

        data = json.loads(result)
        assert "domains" in data
        assert data["domains"][0]["domain"] == "Python"

    @pytest.mark.asyncio
    async def test_execute_no_state_attrs_returns_active_message(self):
        soul = MagicMock()
        soul.state = MagicMock(spec=[])  # no mood/energy/social_battery attrs
        soul.self_model = None
        tool = SoulStatusTool(soul)

        result = await tool.execute()

        assert "active" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_soul_failure(self):
        soul = MagicMock()
        type(soul).state = property(fget=lambda s: (_ for _ in ()).throw(RuntimeError("broken")))
        tool = SoulStatusTool(soul)

        result = await tool.execute()

        assert "Error:" in result


# ---------------------------------------------------------------------------
# Tool definitions via BaseTool.definition
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    """Verify that tool.definition exports correct JSON Schema via BaseTool."""

    def test_remember_tool_definition_name(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        assert tool.definition.name == "soul_remember"

    def test_recall_tool_definition_name(self, mock_soul):
        tool = SoulRecallTool(mock_soul)
        assert tool.definition.name == "soul_recall"

    def test_edit_core_tool_definition_name(self, mock_soul):
        tool = SoulEditCoreTool(mock_soul)
        assert tool.definition.name == "soul_edit_core"

    def test_status_tool_definition_name(self, mock_soul):
        tool = SoulStatusTool(mock_soul)
        assert tool.definition.name == "soul_status"

    def test_definition_has_description(self, mock_soul):
        for cls in (SoulRememberTool, SoulRecallTool, SoulEditCoreTool, SoulStatusTool):
            tool = cls(mock_soul)
            assert len(tool.definition.description) > 10

    def test_anthropic_schema_format(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        schema = tool.definition.to_anthropic_schema()

        assert "name" in schema
        assert "description" in schema
        assert "input_schema" in schema

    def test_openai_schema_format(self, mock_soul):
        tool = SoulRememberTool(mock_soul)
        schema = tool.definition.to_openai_schema()

        assert schema["type"] == "function"
        assert "function" in schema
        assert schema["function"]["name"] == "soul_remember"
