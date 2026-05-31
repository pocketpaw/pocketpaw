"""Specialist-internal tool wrappers — workspace closure, schema, return shape."""

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.agent.pocket_specialist.tools import (
    make_list_pockets_tool,
    make_persist_pocket_tool,
    make_validate_spec_tool,
)


class TestListPocketsTool:
    @pytest.mark.asyncio
    async def test_closes_over_workspace_and_user(self):
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._agent_list_pockets",
            new=AsyncMock(return_value=[{"id": "p1", "name": "X"}]),
        ) as mocked:
            tool = make_list_pockets_tool(workspace_id="ws-1", user_id="user-A")
            result = await tool.ainvoke({})
            mocked.assert_awaited_once_with("ws-1", "user-A")
            assert result == [{"id": "p1", "name": "X"}]


class TestValidateSpecTool:
    @pytest.mark.asyncio
    async def test_returns_warnings_list(self):
        # Fake manifest declaring `timeline` with `events`/`maxItems` but
        # NOT `maxItem`, so a `timeline` with `maxItem` triggers an
        # unknown-prop warning.
        fake_manifest = {
            "schema": "ripple.manifest/v1",
            "widgets": [
                {"type": "timeline", "props": {"events": {}, "maxItems": {}}},
                {"type": "text", "props": {"value": {}}},
            ],
        }
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._get_manifest",
            new=AsyncMock(return_value=fake_manifest),
        ):
            tool = make_validate_spec_tool()
            bad_spec = {
                "version": "1.0",
                "ui": {
                    "type": "timeline",
                    "props": {"events": [], "maxItem": 5},
                },
            }
            result = await tool.ainvoke({"spec": bad_spec})
            assert result["ok"] is False
            assert len(result["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_clean_spec_returns_ok(self):
        fake_manifest = {
            "schema": "ripple.manifest/v1",
            "widgets": [
                {"type": "input", "props": {"value": {}}},
                {"type": "text", "props": {"value": {}}},
            ],
        }
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._get_manifest",
            new=AsyncMock(return_value=fake_manifest),
        ):
            tool = make_validate_spec_tool()
            good_spec = {
                "version": "1.0",
                "state": {"name": ""},
                "ui": {"type": "input", "props": {"value": "{state.name}"}},
            }
            result = await tool.ainvoke({"spec": good_spec})
            assert result["ok"] is True
            assert result["warnings"] == []


class TestPersistPocketTool:
    @pytest.mark.asyncio
    async def test_create_path(self):
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._agent_create",
            new=AsyncMock(return_value=({"id": "new-1", "name": "Created"}, "new-1", None)),
        ) as mocked:
            tool = make_persist_pocket_tool(workspace_id="ws-1", user_id="user-A")
            result = await tool.ainvoke(
                {
                    "name": "Created",
                    "ripple_spec": {"version": "1.0", "ui": {"type": "text"}},
                }
            )
            mocked.assert_awaited_once()
            kwargs = mocked.await_args.kwargs
            assert kwargs["workspace_id"] == "ws-1"
            assert kwargs["owner_id"] == "user-A"
            assert kwargs["name"] == "Created"
            assert result["id"] == "new-1"

    @pytest.mark.asyncio
    async def test_update_path(self):
        with patch(
            "pocketpaw_ee.agent.pocket_specialist.tools._agent_update",
            new=AsyncMock(return_value=({"id": "p1", "name": "Updated"}, None)),
        ) as mocked:
            tool = make_persist_pocket_tool(workspace_id="ws-1", user_id="user-A")
            result = await tool.ainvoke(
                {
                    "target_pocket_id": "p1",
                    "ripple_spec": {"version": "1.0", "ui": {"type": "text"}},
                }
            )
            mocked.assert_awaited_once()
            call = mocked.await_args
            assert call.kwargs.get("pocket_id") == "p1"
            assert result["id"] == "p1"
