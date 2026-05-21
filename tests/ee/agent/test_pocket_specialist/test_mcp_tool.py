"""MCP server registration + handler tests."""

from unittest.mock import AsyncMock, patch

import pytest


class TestPocketSpecialistMcpServer:
    def test_server_name_and_tool_id(self):
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import (
            CREATE_TOOL_ID,
            POCKET_SPECIALIST_TOOL_IDS,
            SERVER_NAME,
        )

        assert SERVER_NAME == "pocketpaw_pocket_specialist"
        assert CREATE_TOOL_ID == "mcp__pocketpaw_pocket_specialist__create"
        assert CREATE_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS

    def test_build_server_returns_object(self):
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import (
            build_pocket_specialist_server,
        )

        server = build_pocket_specialist_server()
        # Just check it's a non-None object — exact type depends on the
        # claude-agent-sdk version.
        assert server is not None


class TestCreateHandler:
    @pytest.mark.asyncio
    async def test_handler_calls_run_specialist_and_returns_text_payload(self):
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _create_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

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
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.run_specialist",
                new=AsyncMock(return_value=fake_out),
            ),
        ):
            payload = await _create_handler({"brief": "Track my repos"})

        assert "content" in payload
        assert payload["content"][0]["type"] == "text"
        assert "p-1" in payload["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_handler_returns_error_when_no_workspace_context(self):
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _create_handler

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.current_user_id",
                return_value=None,
            ),
        ):
            payload = await _create_handler({"brief": "x"})

        assert payload.get("is_error") is True
        assert "workspace" in payload["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_handler_returns_error_when_run_specialist_raises(self):
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _create_handler

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.run_specialist",
                new=AsyncMock(side_effect=RuntimeError("backend exploded")),
            ),
        ):
            payload = await _create_handler({"brief": "Test brief here"})
        assert payload.get("is_error") is True
        assert "backend exploded" in payload["content"][0]["text"].lower()
