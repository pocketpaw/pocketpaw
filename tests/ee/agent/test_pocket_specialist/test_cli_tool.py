"""CLI shell command tests for cloud_pocket_specialist_create.

The CLI dispatcher in ``src/pocketpaw/tools/cli.py`` calls cloud handlers
with a single ``dict`` argument and returns whatever JSON-serialisable
object the handler produces (``_run_cloud_handler`` does the json.dumps
itself). This test mirrors that contract — handlers take a dict, return
a dict.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestCloudPocketSpecialistCreate:
    @pytest.mark.asyncio
    async def test_parses_brief_and_returns_dict(self):
        from pocketpaw_ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "X"},
            warnings=[],
            duration_ms=10,
            backend_used="deep_agents",
        )
        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.run_specialist",
                new=AsyncMock(return_value=fake_out),
            ),
        ):
            result = await _cloud_pocket_specialist_create(
                {"brief": "Track my GitHub PRs across foo/bar/baz repos"}
            )
        assert result["ok"] is True
        assert result["pocket"]["id"] == "p-1"
        assert result["action"] == "created"
        assert result["backend_used"] == "deep_agents"

    @pytest.mark.asyncio
    async def test_parses_hints(self):
        from pocketpaw_ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "PR Tracker"},
            warnings=[],
            duration_ms=10,
            backend_used="deep_agents",
        )
        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.run_specialist",
                new=AsyncMock(return_value=fake_out),
            ) as mock_run,
        ):
            await _cloud_pocket_specialist_create(
                {
                    "brief": "Track my GitHub PRs across teams",
                    "hints": {"name": "PR Tracker", "color": "#0ea5e9"},
                }
            )
        called_input = mock_run.await_args.args[0]
        assert called_input.hints is not None
        assert called_input.hints.name == "PR Tracker"
        assert called_input.hints.color == "#0ea5e9"

    @pytest.mark.asyncio
    async def test_falls_back_to_args_for_workspace_user(self):
        """When ContextVar accessors return None, the handler should use
        workspace_id / user_id provided in the args dict (matches the
        env-var fallback pattern used by other cloud_* handlers like
        _cloud_list_pockets)."""
        from pocketpaw_ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        fake_out = PocketSpecialistCreateOutput(
            ok=True,
            action="created",
            pocket={"id": "p-1", "name": "X"},
            warnings=[],
            duration_ms=10,
            backend_used="deep_agents",
        )
        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_user_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.run_specialist",
                new=AsyncMock(return_value=fake_out),
            ) as mock_run,
        ):
            result = await _cloud_pocket_specialist_create(
                {
                    "brief": "Track my GitHub PRs across teams",
                    "workspace_id": "ws-arg",
                    "user_id": "user-arg",
                }
            )
        assert result["ok"] is True
        assert mock_run.await_args.kwargs["workspace_id"] == "ws-arg"
        assert mock_run.await_args.kwargs["user_id"] == "user-arg"

    @pytest.mark.asyncio
    async def test_returns_error_when_no_workspace_context(self):
        from pocketpaw_ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_user_id",
                return_value=None,
            ),
        ):
            result = await _cloud_pocket_specialist_create({"brief": "A test brief here"})
        assert result["ok"] is False
        assert "workspace" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_handles_run_specialist_exception(self):
        from pocketpaw_ee.agent.pocket_specialist.cli_tool import (
            _cloud_pocket_specialist_create,
        )

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_workspace_id",
                return_value="ws-1",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.current_user_id",
                return_value="user-A",
            ),
            patch(
                "pocketpaw_ee.agent.pocket_specialist.cli_tool.run_specialist",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            result = await _cloud_pocket_specialist_create({"brief": "Track my GitHub PRs"})
        assert result["ok"] is False
        assert "boom" in result.get("error", "")
