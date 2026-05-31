"""Tests for ``PocketSpecialistTool`` and its bridge-driven wiring.

The specialist is registered as a ``BaseTool`` so it flows through the
existing ``tool_bridge`` adapter pattern. These tests cover:

  1. The tool's surface (name, schema, args_schema).
  2. ``execute``'s hint normalization across dict / pydantic / str shapes.
  3. ``_run_handler``'s identity check, success path, error envelope.
  4. The bridge auto-injects it for MCP-capable function-tool backends
     and excludes it for shell-CLI bridge backends.
  5. Each MCP-capable backend's ``_build_custom_tools`` ends up with
     ``pocket_specialist__create`` in the resulting tool list.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


class TestPocketSpecialistTool:
    def test_name_and_description(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import (
            TOOL_DESCRIPTION,
            TOOL_NAME,
            PocketSpecialistTool,
        )

        tool = PocketSpecialistTool()
        assert tool.name == TOOL_NAME == "pocket_specialist__create"
        assert tool.description == TOOL_DESCRIPTION

    def test_parameters_schema_carries_nested_hints(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import PocketSpecialistTool

        params = PocketSpecialistTool().parameters
        assert params["type"] == "object"
        assert "brief" in params["properties"]
        assert params["properties"]["brief"]["type"] == "string"
        assert "hints" in params["properties"]
        assert params["properties"]["hints"]["type"] == "object"
        assert params["required"] == ["brief"]

    def test_args_schema_is_pydantic_model(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import (
            PocketSpecialistArgs,
            PocketSpecialistTool,
        )
        from pydantic import BaseModel

        tool = PocketSpecialistTool()
        assert tool.args_schema is PocketSpecialistArgs
        assert issubclass(tool.args_schema, BaseModel)


# ---------------------------------------------------------------------------
# Hint normalization
# ---------------------------------------------------------------------------


class TestNormalizeHints:
    def test_none_stays_none(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _normalize_hints

        assert _normalize_hints(None) is None

    def test_empty_string_returns_none(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _normalize_hints

        assert _normalize_hints("") is None
        assert _normalize_hints("   ") is None

    def test_dict_passes_through(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _normalize_hints

        assert _normalize_hints({"name": "Foo"}) == {"name": "Foo"}

    def test_pydantic_model_is_dumped(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import (
            PocketSpecialistHintsModel,
            _normalize_hints,
        )

        model = PocketSpecialistHintsModel(name="Foo", color="#abc")
        result = _normalize_hints(model)
        assert result == {"name": "Foo", "color": "#abc"}

    def test_json_string_is_parsed(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _normalize_hints

        assert _normalize_hints('{"name": "Foo"}') == {"name": "Foo"}

    def test_unparseable_string_drops_to_none(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _normalize_hints

        assert _normalize_hints("not json") is None


# ---------------------------------------------------------------------------
# _run_handler — shared dispatch path
# ---------------------------------------------------------------------------


class TestRunHandler:
    @pytest.mark.asyncio
    async def test_returns_serialized_output_on_success(self):
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistCreateOutput
        from pocketpaw_ee.agent.pocket_specialist.tool import _run_handler

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "X"},
            warnings=[],
            duration_ms=42,
            backend_used="deep_agents",
        )
        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.runtime.run_specialist",
                new=AsyncMock(return_value=fake_out),
            ),
        ):
            payload = await _run_handler("Track my repos — at least 10 chars", None)

        parsed = json.loads(payload)
        assert parsed["ok"] is True
        assert parsed["pocket"]["id"] == "p-1"

    @pytest.mark.asyncio
    async def test_missing_context_returns_error_envelope(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _run_handler

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=None,
            ),
        ):
            payload = await _run_handler("x" * 20, None)

        parsed = json.loads(payload)
        assert parsed["ok"] is False
        assert "workspace" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_run_specialist_failure_returns_error_envelope(self):
        from pocketpaw_ee.agent.pocket_specialist.tool import _run_handler

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.runtime.run_specialist",
                new=AsyncMock(side_effect=RuntimeError("backend exploded")),
            ),
        ):
            payload = await _run_handler("Test brief here long enough", None)

        parsed = json.loads(payload)
        assert parsed["ok"] is False
        assert "backend exploded" in parsed["error"]


# ---------------------------------------------------------------------------
# Bridge injection
# ---------------------------------------------------------------------------


class TestBridgeInjection:
    """``_instantiate_all_tools`` adds the specialist for MCP-capable
    function-tool backends only."""

    @pytest.mark.parametrize("backend", ["deep_agents", "google_adk", "openai_agents"])
    def test_specialist_present_for_function_tool_backends(self, backend):
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools(backend=backend)
        names = [t.name for t in tools]
        assert "pocket_specialist__create" in names, names

    @pytest.mark.parametrize(
        "backend", ["claude_agent_sdk", "codex_cli", "opencode", "copilot_sdk"]
    )
    def test_specialist_absent_for_non_function_tool_backends(self, backend):
        from pocketpaw.agents.tool_bridge import _instantiate_all_tools

        tools = _instantiate_all_tools(backend=backend)
        names = [t.name for t in tools]
        assert "pocket_specialist__create" not in names, names


# ---------------------------------------------------------------------------
# Backend integration — main-agent tool lists include the specialist
# ---------------------------------------------------------------------------


class TestBackendIntegration:
    def test_deep_agents_main_agent_includes_specialist_tool(self):
        pytest.importorskip("deepagents")
        pytest.importorskip("langchain_core")
        from pocketpaw.agents.deep_agents import DeepAgentsBackend
        from pocketpaw.config import Settings

        backend = DeepAgentsBackend(Settings())
        tools = backend._build_custom_tools()
        names = [getattr(t, "name", "") for t in tools]
        assert "pocket_specialist__create" in names

    def test_deep_agents_isolated_specialist_does_not_inject(self):
        pytest.importorskip("deepagents")
        pytest.importorskip("langchain_core")
        from pocketpaw.agents.deep_agents import DeepAgentsBackend
        from pocketpaw.config import Settings

        backend = DeepAgentsBackend(Settings())
        # Simulate isolated specialist setup: pre-populates _custom_tools.
        backend.attach_specialist_tools([])
        tools = backend._build_custom_tools()
        names = [getattr(t, "name", "") for t in tools]
        assert "pocket_specialist__create" not in names

    def test_google_adk_main_agent_includes_specialist_tool(self):
        pytest.importorskip("google.adk")
        from pocketpaw.agents.google_adk import GoogleADKBackend
        from pocketpaw.config import Settings

        backend = GoogleADKBackend(Settings())
        tools = backend._build_custom_tools()
        names = [getattr(t, "name", None) or t.func.__name__ for t in tools]
        assert "pocket_specialist__create" in names

    def test_openai_agents_main_agent_includes_specialist_tool(self):
        pytest.importorskip("agents")
        from pocketpaw.agents.openai_agents import OpenAIAgentsBackend
        from pocketpaw.config import Settings

        backend = OpenAIAgentsBackend(Settings())
        tools = backend._build_custom_tools()
        names = [getattr(t, "name", "") for t in tools]
        assert "pocket_specialist__create" in names
