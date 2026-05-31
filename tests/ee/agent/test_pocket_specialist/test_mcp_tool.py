# test_mcp_tool.py — MCP server registration + handler tests.
# Updated: 2026-05-23 (fix/pocket-edit-not-visible) — added the
#   ``TestEditHandler`` class covering the regression where the
#   ``pocket_specialist__edit`` MCP tool returned a "successful"
#   tool result (no ``is_error`` flag) even when the underlying
#   ``PocketSpecialistEditOutput.ok`` was False — typically because
#   every supplied granular op was rejected by the service (e.g.
#   ``add_node`` with a stale ``parent_id``). The calling chat
#   agent then frequently fabricated a confident "applied" reply
#   while the canvas stayed stale. Both ``_edit_handler`` and
#   ``_create_handler`` now flip ``is_error`` whenever ``out.ok``
#   is False, so the tool-use framework surfaces the rejection.
"""MCP server registration + handler tests."""

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


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

    @pytest.mark.asyncio
    async def test_handler_flips_is_error_when_specialist_returns_ok_false(self):
        """A specialist run that completes cleanly but reports ``ok=False``
        must still come back as an MCP tool error so the chat agent can't
        skim past the rejection. Mirror of the edit-side regression — the
        create path has the same hallucination surface."""
        import json

        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _create_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        failed_out = PocketSpecialistCreateOutput(
            ok=False,
            action="failed",
            pocket=None,
            warnings=[],
            duration_ms=10,
            backend_used="agent_mode",
            error="catalog gate rejected widget type 'imaginary-card'",
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
                new=AsyncMock(return_value=failed_out),
            ),
        ):
            payload = await _create_handler({"brief": "Build a thing"})

        assert payload.get("is_error") is True, (
            "ok=False from the specialist must surface as is_error=True so "
            "the chat agent treats the tool call as a failure"
        )
        body = json.loads(payload["content"][0]["text"])
        assert body["ok"] is False
        assert "imaginary-card" in body["error"]

    @pytest.mark.asyncio
    async def test_handler_does_not_flag_is_error_on_draft_kit(self):
        """``ok=False, action="draft_kit"`` is the agent-mode first-call
        HANDSHAKE — the adapter returns the draft kit so the chat agent
        can compute granular ops and call back. Flagging it as is_error
        breaks the two-call flow silently."""
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _create_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        draft = PocketSpecialistCreateOutput(
            ok=False,
            action="draft_kit",
            pocket=None,
            duration_ms=10,
            backend_used="agent_mode",
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
                new=AsyncMock(return_value=draft),
            ),
        ):
            payload = await _create_handler({"brief": "Build a thing"})

        assert "is_error" not in payload, (
            "draft_kit is a protocol handshake, not a failure — flagging it "
            "breaks the agent-mode two-call flow"
        )

    @pytest.mark.asyncio
    async def test_handler_does_not_flag_is_error_on_redraft(self):
        """``ok=False, action="redraft"`` is the create validation retry
        loop — the chat agent must see the redraft signal to retry, not
        treat it as a hard tool failure."""
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _create_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateOutput,
        )

        redraft = PocketSpecialistCreateOutput(
            ok=False,
            action="redraft",
            pocket=None,
            duration_ms=10,
            backend_used="agent_mode",
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
                new=AsyncMock(return_value=redraft),
            ),
        ):
            payload = await _create_handler({"brief": "Build a thing"})

        assert "is_error" not in payload, (
            "redraft is the create validation retry signal — flagging it breaks the retry loop"
        )


class TestEditHandler:
    """The bug: ``add_node`` with a stale ``parent_id`` was rejected by
    the service (correct), the agent-mode adapter returned
    ``ok=False, action="failed", warnings=[<reason>]`` (correct), the
    MCP tool serialised the body and returned it WITHOUT ``is_error``
    (incorrect — looked like a successful tool call). The chat agent
    then routinely fabricated "1 op applied" while the canvas stayed
    stale because nothing actually persisted. These tests pin
    ``is_error=True`` on the MCP response whenever ``out.ok`` is False
    so the rejection cannot slip past the tool-use framework."""

    @pytest.mark.asyncio
    async def test_handler_flips_is_error_when_edit_run_reports_ok_false(self):
        """The captain's bug: every op rejected → ok=False → is_error=True."""
        import json

        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _edit_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditOutput,
        )

        failed_out = PocketSpecialistEditOutput(
            ok=False,
            action="failed",
            pocket_id="p1",
            ops=[],
            duration_ms=10,
            backend_used="agent_mode",
            error=(
                "No edit ops were applied — every supplied op was "
                "rejected or unsupported. See warnings for the per-op "
                "reasons."
            ),
            warnings=["Edit op 'add_node' could not be applied: no node with id 'n_1nchlml3'"],
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
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.run_edit_specialist",
                new=AsyncMock(return_value=failed_out),
            ),
        ):
            payload = await _edit_handler(
                {
                    "pocket_id": "p1",
                    "intent": "Add another line chart",
                    "ops": [
                        {
                            "op": "add_node",
                            "args": {
                                "parent_id": "n_1nchlml3",
                                "spec": {"type": "chart"},
                            },
                        }
                    ],
                }
            )

        assert payload.get("is_error") is True, (
            "ok=False must surface as is_error=True so the chat agent "
            "can't claim success while the canvas stays stale"
        )
        body = json.loads(payload["content"][0]["text"])
        assert body["ok"] is False
        assert body["action"] == "failed"
        # The warnings must still ride along so the agent has the reason
        # — flipping is_error only changes how the framework presents it.
        assert any("n_1nchlml3" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_handler_keeps_success_path_unflagged(self):
        """An edit run that actually applied an op must NOT trip
        ``is_error`` — that would make a real apply look like a failure
        and trigger spurious retries by the framework."""
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _edit_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditOutput,
        )

        applied_out = PocketSpecialistEditOutput(
            ok=True,
            action="applied",
            pocket_id="p1",
            ops=[{"op": "set_state", "args": {"path": "filter", "value": "done"}}],
            duration_ms=5,
            backend_used="agent_mode",
            warnings=[],
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
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.run_edit_specialist",
                new=AsyncMock(return_value=applied_out),
            ),
        ):
            payload = await _edit_handler(
                {
                    "pocket_id": "p1",
                    "intent": "Filter to done",
                    "ops": [
                        {
                            "op": "set_state",
                            "args": {"path": "filter", "value": "done"},
                        }
                    ],
                }
            )

        assert "is_error" not in payload, (
            "ok=True must not be flagged as is_error — that would make "
            "every successful apply look like a tool error"
        )

    @pytest.mark.asyncio
    async def test_handler_keeps_decline_path_unflagged(self):
        """A genuine planner decline (``ok=True, ops=[], warnings=[...]``)
        is the subagent path saying "I looked and decided to do nothing"
        — that is a legitimate no-op, not a failure. It must keep
        ``is_error`` off so the chat agent can relay the planner's
        reason without the framework treating it as an error."""
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _edit_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditOutput,
        )

        declined_out = PocketSpecialistEditOutput(
            ok=True,
            action="applied",
            pocket_id="p1",
            ops=[],
            duration_ms=5,
            backend_used="deep_agents",
            warnings=["I couldn't find a chart to recolor — the pocket has no chart node."],
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
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.run_edit_specialist",
                new=AsyncMock(return_value=declined_out),
            ),
        ):
            payload = await _edit_handler({"pocket_id": "p1", "intent": "Make the chart blue"})

        assert "is_error" not in payload, "A planner that legitimately did nothing is not a failure"

    @pytest.mark.asyncio
    async def test_handler_does_not_flag_is_error_on_draft_kit(self):
        """``ok=False, action="draft_kit"`` is the edit agent-mode
        first-call HANDSHAKE — the adapter returns the draft kit so
        the chat agent can compute granular ops and call back.
        Flagging it as is_error breaks the two-call flow silently."""
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import _edit_handler
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditOutput,
        )

        draft = PocketSpecialistEditOutput(
            ok=False,
            action="draft_kit",
            pocket_id="p1",
            ops=[],
            duration_ms=10,
            backend_used="agent_mode",
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
                "pocketpaw_ee.agent.pocket_specialist.mcp_tool.run_edit_specialist",
                new=AsyncMock(return_value=draft),
            ),
        ):
            payload = await _edit_handler({"pocket_id": "p1", "intent": "add a chart"})

        assert "is_error" not in payload, (
            "draft_kit is a protocol handshake, not a failure — flagging it "
            "breaks the agent-mode two-call flow"
        )

    # NOTE: An end-to-end variant of the captain's bug — running real
    # agent-mode ``_apply_ops`` against a real (mongomock) pocket with
    # an ``add_node`` op naming a non-existent ``parent_id`` — lives in
    # ``tests/cloud/test_pocket_edit_failure_visibility.py`` because the
    # mongo_db fixture is only wired up under tests/cloud/.
