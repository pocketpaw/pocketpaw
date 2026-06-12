# tests/ee/agent/test_connectors_mcp_server/test_mcp_tool.py
# Created: 2026-06-08 (feat/connector-mcp-execution / keystone) — coverage for
#   the in-process ``pocketpaw_connectors`` MCP server. Mirrors the
#   test_sites_mcp_server layout: registration assertions (server name, tool
#   ids, build, provider allowlist publication, ambient-not-opt-in) plus
#   per-handler tests that mock the identity ContextVars + the connectors
#   service and inspect the MCP envelope the SDK returns. The handler tests
#   prove the v1 contract: read (auto-trust) actions reach service.execute,
#   write (confirm-trust) actions are blocked WITHOUT calling execute,
#   connectors not bound to the pocket are rejected, and a missing pocket /
#   workspace ContextVar (called off-stream) yields a clear error.
"""MCP server registration + handler tests for connector execution."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.connectors.domain import (  # noqa: E402
    ConnectorActionInfo,
    PocketConnectorInfo,
)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestConnectorsMcpServerRegistration:
    def test_server_name_and_tool_ids(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.connectors import (
            CONNECTOR_EXECUTE_TOOL_ID,
            CONNECTOR_TOOL_IDS,
            LIST_CONNECTOR_ACTIONS_TOOL_ID,
            SERVER_NAME,
        )

        assert SERVER_NAME == "pocketpaw_connectors"
        # Allowlist entries must use the exact ``mcp__<server>__<tool>`` form.
        assert LIST_CONNECTOR_ACTIONS_TOOL_ID == "mcp__pocketpaw_connectors__list_connector_actions"
        assert CONNECTOR_EXECUTE_TOOL_ID == "mcp__pocketpaw_connectors__connector_execute"
        assert LIST_CONNECTOR_ACTIONS_TOOL_ID in CONNECTOR_TOOL_IDS
        assert CONNECTOR_EXECUTE_TOOL_ID in CONNECTOR_TOOL_IDS
        # The server now also carries the two Sense-tier tools (chunk 4).
        assert len(CONNECTOR_TOOL_IDS) == 4

    def test_extension_provider_advertises_tool_ids(self) -> None:
        """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
        allowlist loop — both tool ids must come through it."""
        from pocketpaw_ee.agent.mcp_servers.connectors import CONNECTOR_TOOL_IDS
        from pocketpaw_ee.extensions import CloudConnectorsMcpProvider

        advertised = CloudConnectorsMcpProvider().tool_ids()
        for tid in CONNECTOR_TOOL_IDS:
            assert tid in advertised

    def test_provider_build_server_matches_shape(self) -> None:
        from pocketpaw_ee.extensions import CloudConnectorsMcpProvider

        out = CloudConnectorsMcpProvider().build_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_connectors"
            assert server is not None

    def test_build_server_returns_object(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.connectors import build_connectors_context_server

        out = build_connectors_context_server()
        if out is not None:
            name, server = out
            assert name == "pocketpaw_connectors"
            assert server is not None

    def test_provider_is_ambient_not_opt_in(self) -> None:
        """The connectors server must NOT be opt-in — the M3-derived connector
        skills reach connector_execute ambiently, with no per-agent opt-in."""
        from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

        assert "pocketpaw_connectors" not in OPT_IN_MCP_SERVERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payload(envelope: dict) -> dict:
    """MCP success responses pack the JSON body into ``content[0].text``."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


def _patch_identity(workspace_id: str | None, user_id: str | None, pocket_id: str | None):
    """Patch the per-stream identity accessors the handler reads via
    ``_identity`` (imported function-locally from agent_service)."""
    return (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=workspace_id,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
            return_value=user_id,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_pocket_id",
            return_value=pocket_id,
        ),
    )


def _github_pocket_info() -> PocketConnectorInfo:
    """A GitHub connector with one read action and one write action."""
    return PocketConnectorInfo(
        name="github",
        display_name="GitHub",
        type="developer",
        icon="git-branch",
        actions=(
            ConnectorActionInfo(
                name="list_issues",
                description="List issues for a repository",
                trust_level="auto",
                execution_mode="cloud",
                is_read=True,
            ),
            ConnectorActionInfo(
                name="create_issue",
                description="Create a new issue",
                trust_level="confirm",
                execution_mode="cloud",
                is_read=False,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Handler — list_connector_actions
# ---------------------------------------------------------------------------


class TestListConnectorActionsHandler:
    @pytest.mark.asyncio
    async def test_lists_read_actions_and_marks_writes_blocked(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.list_pocket_connectors",
                new=AsyncMock(return_value=[_github_pocket_info()]),
            ) as mock_list,
        ):
            out = await connectors_mcp._list_connector_actions_handler({})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["pocket_id"] == "pk_1"
        assert len(body["connectors"]) == 1
        gh = body["connectors"][0]
        assert gh["connector"] == "github"
        # Read action listed as runnable.
        assert [a["action"] for a in gh["read_actions"]] == ["list_issues"]
        # Write action listed but flagged blocked.
        assert len(gh["write_actions_blocked"]) == 1
        assert gh["write_actions_blocked"][0]["action"] == "create_issue"
        assert "v2" in gh["write_actions_blocked"][0]["status"]
        mock_list.assert_awaited_once_with("ws_1", "pk_1")

    @pytest.mark.asyncio
    async def test_no_pocket_falls_through_to_workspace_scope(self) -> None:
        """Unanchored chats (pocket_id=None) query the service with pocket ""
        so workspace-scoped connectors stay reachable from any chat."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", None)
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.list_pocket_connectors",
                new=AsyncMock(return_value=[]),
            ) as mock_list,
        ):
            out = await connectors_mcp._list_connector_actions_handler({})

        body = _decode_payload(out)
        assert body["connectors"] == []
        assert "No connectors are reachable" in body["message"]
        mock_list.assert_awaited_once_with("ws_1", "")

    @pytest.mark.asyncio
    async def test_no_connectors_returns_clear_message(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.list_pocket_connectors",
                new=AsyncMock(return_value=[]),
            ),
        ):
            out = await connectors_mcp._list_connector_actions_handler({})

        body = _decode_payload(out)
        assert body["connectors"] == []
        assert "No connectors are reachable" in body["message"]

    @pytest.mark.asyncio
    async def test_no_workspace_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity(None, None, None)
        with ws_patch, user_patch, pocket_patch:
            out = await connectors_mcp._list_connector_actions_handler({})

        assert out.get("is_error") is True
        assert "no active workspace" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# Handler — connector_execute
# ---------------------------------------------------------------------------


class TestConnectorExecuteHandler:
    @pytest.mark.asyncio
    async def test_read_action_calls_service_execute(self) -> None:
        """An auto-trust (read) action reaches service.execute and returns the
        result."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp
        from pocketpaw_ee.cloud.connectors.dto import ExecuteActionResponse

        read_trust = ConnectorActionInfo(
            name="list_issues",
            description="List issues",
            trust_level="auto",
            execution_mode="cloud",
            is_read=True,
        )
        exec_result = ExecuteActionResponse(
            success=True,
            data=[{"number": 1, "title": "first issue"}],
            execution_mode="cloud",
        )

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.get_action_trust",
                new=AsyncMock(return_value=read_trust),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.execute",
                new=AsyncMock(return_value=exec_result),
            ) as mock_execute,
        ):
            out = await connectors_mcp._connector_execute_handler(
                {
                    "connector_name": "github",
                    "action": "list_issues",
                    "params": {"owner": "acme", "repo": "api"},
                }
            )

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["executed"] is True
        assert body["success"] is True
        assert body["data"][0]["title"] == "first issue"
        # The execute call carried the pocket id + scope.
        mock_execute.assert_awaited_once()
        call = mock_execute.await_args
        assert call.args[0] == "ws_1"  # workspace_id
        assert call.args[1] == "github"  # name
        req = call.args[2]
        assert req.action == "list_issues"
        assert req.scope == "pocket"
        assert req.pocket_id == "pk_1"

    @pytest.mark.asyncio
    async def test_write_action_blocked_without_calling_execute(self) -> None:
        """A confirm-trust (write) action is refused and service.execute is
        NEVER called."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        write_trust = ConnectorActionInfo(
            name="create_issue",
            description="Create an issue",
            trust_level="confirm",
            execution_mode="cloud",
            is_read=False,
        )

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.get_action_trust",
                new=AsyncMock(return_value=write_trust),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.execute",
                new=AsyncMock(),
            ) as mock_execute,
        ):
            out = await connectors_mcp._connector_execute_handler(
                {"connector_name": "github", "action": "create_issue", "params": {}}
            )

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["executed"] is False
        assert body["blocked"] is True
        assert "needs approval" in body["reason"]
        assert "v2" in body["reason"]
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connector_not_bound_is_rejected(self) -> None:
        """A connector not bound to THIS pocket is rejected before trust lookup
        or execute."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.get_action_trust",
                new=AsyncMock(),
            ) as mock_trust,
            patch(
                "pocketpaw_ee.cloud.connectors.service.execute",
                new=AsyncMock(),
            ) as mock_execute,
        ):
            out = await connectors_mcp._connector_execute_handler(
                {"connector_name": "github", "action": "list_issues", "params": {}}
            )

        assert out.get("is_error") is True
        assert "not bound to this pocket" in out["content"][0]["text"]
        mock_trust.assert_not_awaited()
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_action_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.get_action_trust",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "pocketpaw_ee.cloud.connectors.service.execute",
                new=AsyncMock(),
            ) as mock_execute,
        ):
            out = await connectors_mcp._connector_execute_handler(
                {"connector_name": "github", "action": "no_such_action", "params": {}}
            )

        assert out.get("is_error") is True
        assert "no action 'no_such_action'" in out["content"][0]["text"]
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_stream_no_workspace_is_error(self) -> None:
        """Called outside an SSE chat stream (no ContextVars) → clear error,
        no service touched."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity(None, None, None)
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.connectors.service.execute",
                new=AsyncMock(),
            ) as mock_execute,
        ):
            out = await connectors_mcp._connector_execute_handler(
                {"connector_name": "github", "action": "list_issues"}
            )

        assert out.get("is_error") is True
        assert "no active workspace" in out["content"][0]["text"]
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_connector_name_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with ws_patch, user_patch, pocket_patch:
            out = await connectors_mcp._connector_execute_handler({"action": "list_issues"})

        assert out.get("is_error") is True
        assert "connector_name is required" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# Handlers — Sense tier (list_senses / sense_execute)
# ---------------------------------------------------------------------------


class TestSenseTools:
    @pytest.mark.asyncio
    async def test_list_senses_reports_only_resolvable_senses(self) -> None:
        """list_senses returns the senses that resolve to an enabled connector
        for the workspace, and skips the ones with no provider."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp
        from pocketpaw_ee.cloud.senses.resolver import ResolvedSense

        async def fake_resolve_many(sense_ids, workspace_id, *, pocket_id=None):
            return {
                sid: (
                    ResolvedSense(
                        sense_id="paw.email.v1",
                        connector_name="gmail",
                        ambiguous=False,
                        candidates=["gmail"],
                    )
                    if sid == "paw.email.v1"
                    else None  # no provider for the rest
                )
                for sid in sense_ids
            }

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.senses.resolver.resolve_many",
                new=AsyncMock(side_effect=fake_resolve_many),
            ),
        ):
            out = await connectors_mcp._list_senses_handler({})

        assert not out.get("is_error")
        body = _decode_payload(out)
        ids = [s["sense"] for s in body["senses"]]
        assert ids == ["paw.email.v1"]
        assert body["senses"][0]["connector"] == "gmail"

    @pytest.mark.asyncio
    async def test_list_senses_no_workspace_errors(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        ws_patch, user_patch, pocket_patch = _patch_identity(None, None, None)
        with ws_patch, user_patch, pocket_patch:
            out = await connectors_mcp._list_senses_handler({})

        assert out.get("is_error") is True
        assert "no active workspace" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_sense_execute_success_flattens_response(self) -> None:
        """A successful sense_execute flattens the underlying
        ExecuteActionResponse into the SAME shape connector_execute returns —
        not a nested Pydantic repr (json.dumps uses default=str)."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp
        from pocketpaw_ee.cloud.connectors.dto import ExecuteActionResponse
        from pocketpaw_ee.cloud.senses.resolver import SenseExecutionResult

        exec_result = SenseExecutionResult(
            ok=True,
            sense_id="paw.email.v1",
            connector_name="gmail",
            action="gmail_search",
            data=ExecuteActionResponse(
                success=True,
                data=[{"id": "m1", "subject": "hello"}],
                execution_mode="cloud",
            ),
        )

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.senses.resolver.execute_sense",
                new=AsyncMock(return_value=exec_result),
            ) as mock_exec,
        ):
            out = await connectors_mcp._sense_execute_handler(
                {"sense": "paw.email.v1", "action": "gmail_search", "params": {"q": "hi"}}
            )

        assert not out.get("is_error")
        body = _decode_payload(out)
        # Flat shape, structurally identical to connector_execute's success.
        assert body["executed"] is True
        assert body["sense"] == "paw.email.v1"
        assert body["connector"] == "gmail"
        assert body["success"] is True
        assert body["data"][0]["subject"] == "hello"
        assert body["execution_mode"] == "cloud"
        # Delegated to execute_sense with the resolved identity.
        mock_exec.assert_awaited_once()
        call = mock_exec.await_args
        assert call.args[0] == "paw.email.v1"  # sense
        assert call.args[1] == "gmail_search"  # action
        assert call.kwargs["pocket_id"] == "pk_1"
        assert call.kwargs["user_id"] == "u_1"

    @pytest.mark.asyncio
    async def test_sense_execute_refusal_is_delegated_not_self_gated(self) -> None:
        """A write/needs-approval refusal comes back as an error envelope, and
        the handler still DELEGATED to execute_sense (it does not gate itself)."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp
        from pocketpaw_ee.cloud.senses.resolver import SenseExecutionResult

        refused = SenseExecutionResult(
            ok=False,
            sense_id="paw.email.v1",
            connector_name="gmail",
            action="gmail_send",
            error="sense.action_needs_approval",
            message="action 'gmail_send' needs approval — not executed in v1 (read-first).",
        )

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.senses.resolver.execute_sense",
                new=AsyncMock(return_value=refused),
            ) as mock_exec,
        ):
            out = await connectors_mcp._sense_execute_handler(
                {"sense": "paw.email.v1", "action": "gmail_send", "params": {}}
            )

        assert out.get("is_error") is True
        assert "needs approval" in out["content"][0]["text"]
        mock_exec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sense_execute_no_provider_errors(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp
        from pocketpaw_ee.cloud.senses.resolver import SenseExecutionResult

        none_result = SenseExecutionResult(
            ok=False,
            sense_id="paw.email.v1",
            action="gmail_search",
            error="sense.no_provider",
            message="no enabled connector can fill 'paw.email.v1' for this workspace.",
        )

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.senses.resolver.execute_sense",
                new=AsyncMock(return_value=none_result),
            ),
        ):
            out = await connectors_mcp._sense_execute_handler(
                {"sense": "paw.email.v1", "action": "gmail_search"}
            )

        assert out.get("is_error") is True
        assert "no enabled connector" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_sense_execute_guards_and_validation(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp
        from pocketpaw_ee.cloud.senses.resolver import SenseExecutionResult

        # No pocket → flows through with pocket_id=None (workspace-scoped
        # providers stay reachable from unanchored chats, matching list_senses).
        ws_p, u_p, pk_p = _patch_identity("ws_1", "u_1", None)
        mock_exec = AsyncMock(
            return_value=SenseExecutionResult(
                ok=True, sense_id="paw.email.v1", action="gmail_search"
            )
        )
        with (
            ws_p,
            u_p,
            pk_p,
            patch("pocketpaw_ee.cloud.senses.resolver.execute_sense", new=mock_exec),
        ):
            out = await connectors_mcp._sense_execute_handler(
                {"sense": "paw.email.v1", "action": "gmail_search"}
            )
        assert out.get("is_error") is not True
        assert mock_exec.await_args.kwargs["pocket_id"] is None

        # Missing sense → refused.
        ws_p, u_p, pk_p = _patch_identity("ws_1", "u_1", "pk_1")
        with ws_p, u_p, pk_p:
            out = await connectors_mcp._sense_execute_handler({"action": "gmail_search"})
        assert out.get("is_error") is True
        assert "sense is required" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_sense_execute_unknown_sense_is_clean_error(self) -> None:
        """An unknown paw.* id raises SenseValidationError inside execute_sense;
        the handler turns it into a clean error, not a crash."""
        from pocketpaw_ee.agent.mcp_servers import connectors as connectors_mcp

        from pocketpaw.senses import SenseValidationError

        ws_patch, user_patch, pocket_patch = _patch_identity("ws_1", "u_1", "pk_1")
        with (
            ws_patch,
            user_patch,
            pocket_patch,
            patch(
                "pocketpaw_ee.cloud.senses.resolver.execute_sense",
                new=AsyncMock(side_effect=SenseValidationError("unknown core sense id")),
            ),
        ):
            out = await connectors_mcp._sense_execute_handler(
                {"sense": "paw.unknown.v1", "action": "x"}
            )

        assert out.get("is_error") is True
        assert "unknown sense" in out["content"][0]["text"]
