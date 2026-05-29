# tests/ee/agent/test_foresight_mcp_server/test_mcp_tool.py
# Created: 2026-05-28 — coverage for the in-process ``pocketpaw_foresight``
# MCP server. Mirrors the pocket_specialist test layout: registration
# assertions (server name, tool ids, allowlist publication) plus
# per-tool handler tests that mock the ``agent_context`` wrappers and
# inspect the MCP envelope the SDK returns to the agent.
"""MCP server registration + handler tests for foresight scenario CRUD + run."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


class TestForesightMcpServerRegistration:
    def test_server_name_and_tool_ids(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.foresight import (
            DELETE_SCENARIO_TOOL_ID,
            FORESIGHT_TOOL_IDS,
            GET_AGGREGATE_TOOL_ID,
            GET_BACKTEST_TOOL_ID,
            GET_INSIGHTS_TOOL_ID,
            GET_ONBOARDING_GATE_TOOL_ID,
            GET_RUN_TOOL_ID,
            GET_SCENARIO_TOOL_ID,
            LIST_BACKTESTS_TOOL_ID,
            LIST_PROJECTED_DECISIONS_TOOL_ID,
            LIST_RUNS_TOOL_ID,
            LIST_SCENARIOS_TOOL_ID,
            RUN_SCENARIO_TOOL_ID,
            SAVE_SCENARIO_TOOL_ID,
            SERVER_NAME,
            UPDATE_SCENARIO_TOOL_ID,
        )

        assert SERVER_NAME == "pocketpaw_foresight"
        # Allowlist entries must use the exact ``mcp__<server>__<tool>`` form.
        assert SAVE_SCENARIO_TOOL_ID == "mcp__pocketpaw_foresight__save_scenario"
        assert LIST_SCENARIOS_TOOL_ID == "mcp__pocketpaw_foresight__list_scenarios"
        assert GET_SCENARIO_TOOL_ID == "mcp__pocketpaw_foresight__get_scenario"
        assert UPDATE_SCENARIO_TOOL_ID == "mcp__pocketpaw_foresight__update_scenario"
        assert DELETE_SCENARIO_TOOL_ID == "mcp__pocketpaw_foresight__delete_scenario"
        assert RUN_SCENARIO_TOOL_ID == "mcp__pocketpaw_foresight__run_scenario"
        assert LIST_RUNS_TOOL_ID == "mcp__pocketpaw_foresight__list_runs"
        assert GET_RUN_TOOL_ID == "mcp__pocketpaw_foresight__get_run"
        # Result-side reads — 2026-05-28 follow-up.
        assert (
            LIST_PROJECTED_DECISIONS_TOOL_ID
            == "mcp__pocketpaw_foresight__list_projected_decisions"
        )
        assert GET_AGGREGATE_TOOL_ID == "mcp__pocketpaw_foresight__get_aggregate"
        assert GET_INSIGHTS_TOOL_ID == "mcp__pocketpaw_foresight__get_insights"
        # Backtest-side reads — 2026-05-28 follow-up #2. Read-only per
        # RFC 08 §13.1 (backtest creation stays UI-initiated).
        assert LIST_BACKTESTS_TOOL_ID == "mcp__pocketpaw_foresight__list_backtests"
        assert GET_BACKTEST_TOOL_ID == "mcp__pocketpaw_foresight__get_backtest"
        assert GET_ONBOARDING_GATE_TOOL_ID == "mcp__pocketpaw_foresight__get_onboarding_gate"
        assert len(FORESIGHT_TOOL_IDS) == 14
        # Every id is published — the claude_sdk allowlist loop reads
        # this tuple.
        for tid in (
            SAVE_SCENARIO_TOOL_ID,
            LIST_SCENARIOS_TOOL_ID,
            GET_SCENARIO_TOOL_ID,
            UPDATE_SCENARIO_TOOL_ID,
            DELETE_SCENARIO_TOOL_ID,
            RUN_SCENARIO_TOOL_ID,
            LIST_RUNS_TOOL_ID,
            GET_RUN_TOOL_ID,
            LIST_PROJECTED_DECISIONS_TOOL_ID,
            GET_AGGREGATE_TOOL_ID,
            GET_INSIGHTS_TOOL_ID,
            LIST_BACKTESTS_TOOL_ID,
            GET_BACKTEST_TOOL_ID,
            GET_ONBOARDING_GATE_TOOL_ID,
        ):
            assert tid in FORESIGHT_TOOL_IDS

    def test_extension_provider_advertises_all_tool_ids(self) -> None:
        """The entry-point provider's ``tool_ids()`` feeds the claude_sdk
        allowlist loop — every tool id must come through it."""
        from pocketpaw_ee.agent.mcp_servers.foresight import FORESIGHT_TOOL_IDS
        from pocketpaw_ee.extensions import CloudForesightMcpProvider

        advertised = CloudForesightMcpProvider().tool_ids()
        for tid in FORESIGHT_TOOL_IDS:
            assert tid in advertised

    def test_build_server_returns_object(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.foresight import build_foresight_server

        out = build_foresight_server()
        # When claude_agent_sdk is installed (the ee group), we get back
        # ``(name, server)``. When it isn't, ``None`` — the import-guard
        # path. Either is a valid runtime state.
        if out is not None:
            name, server = out
            assert name == "pocketpaw_foresight"
            assert server is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payload(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``. Decode
    it so the tests can assert on dict fields without re-encoding."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


# ---------------------------------------------------------------------------
# Per-tool handler tests
# ---------------------------------------------------------------------------


class TestListScenariosHandler:
    @pytest.mark.asyncio
    async def test_delegates_to_agent_context_and_returns_items(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "items": [{"id": "s1", "name": "renewal"}],
            "total": 1,
            "limit": 20,
            "offset": 0,
            "has_more": False,
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_scenarios_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._list_scenarios_handler(
                {"limit": 20, "offset": 0, "sub_type": "decision_forecast"}
            )

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["items"][0]["id"] == "s1"
        # ``ok`` is stripped before encoding — it's a transport concern.
        assert "ok" not in body
        mock.assert_awaited_once_with(limit=20, offset=0, sub_type="decision_forecast")

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_scenarios_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._list_scenarios_handler({})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]


class TestSaveScenarioHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "id": "abc123",
            "workspace_id": "w1",
            "name": "renewal",
            "sub_type": "decision_forecast",
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.save_scenario_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._save_scenario_handler(
                {
                    "name": "renewal",
                    "sub_type": "decision_forecast",
                    "yaml_body": "name: renewal\n",
                    "description": "desc",
                }
            )

        body = _decode_payload(out)
        assert body["id"] == "abc123"
        mock.assert_awaited_once_with(
            name="renewal",
            sub_type="decision_forecast",
            yaml_body="name: renewal\n",
            description="desc",
        )

    @pytest.mark.asyncio
    async def test_missing_required_field_returns_local_error(self) -> None:
        """Argument-shape validation happens at the handler before any
        agent_context call — so a missing ``yaml_body`` never reaches
        the cloud."""
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        mock = AsyncMock()
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.save_scenario_for_agent",
            new=mock,
        ):
            out = await foresight_mcp._save_scenario_handler(
                {"name": "x", "sub_type": "decision_forecast"}
            )

        assert out.get("is_error") is True
        assert "yaml_body" in out["content"][0]["text"]
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cloud_error_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": False,
            "error": "foresight.invalid_yaml",
            "message": "YAML parse error: ...",
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.save_scenario_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._save_scenario_handler(
                {
                    "name": "x",
                    "sub_type": "decision_forecast",
                    "yaml_body": "junk",
                }
            )

        assert out.get("is_error") is True
        # The agent reads the code + message verbatim so it can retry
        # with a corrected YAML.
        text = out["content"][0]["text"]
        assert "foresight.invalid_yaml" in text
        assert "YAML parse error" in text


class TestUpdateScenarioHandler:
    @pytest.mark.asyncio
    async def test_passes_all_fields(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.update_scenario_for_agent",
            new=AsyncMock(return_value={"ok": True, "id": "s1", "name": "renamed"}),
        ) as mock:
            out = await foresight_mcp._update_scenario_handler(
                {
                    "scenario_id": "s1",
                    "name": "renamed",
                    "sub_type": "decision_forecast",
                    "yaml_body": "name: renamed\n",
                    "description": "updated",
                }
            )

        assert not out.get("is_error")
        mock.assert_awaited_once_with(
            scenario_id="s1",
            name="renamed",
            sub_type="decision_forecast",
            yaml_body="name: renamed\n",
            description="updated",
        )


class TestGetScenarioHandler:
    @pytest.mark.asyncio
    async def test_requires_scenario_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        out = await foresight_mcp._get_scenario_handler({})
        assert out.get("is_error") is True


class TestDeleteScenarioHandler:
    @pytest.mark.asyncio
    async def test_delegates_and_returns_scenario_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.delete_scenario_for_agent",
            new=AsyncMock(return_value={"ok": True, "scenario_id": "s1"}),
        ) as mock:
            out = await foresight_mcp._delete_scenario_handler({"scenario_id": "s1"})

        body = _decode_payload(out)
        assert body["scenario_id"] == "s1"
        mock.assert_awaited_once_with("s1")


class TestRunScenarioHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_run_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "id": "run-1",
            "status": "complete",
            "scenario_name": "renewal",
            "result": {"aggregates": {"verdict": "go"}},
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.run_scenario_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._run_scenario_handler(
                {
                    "name": "renewal",
                    "custom_scenario_id": "s1",
                    "route_to_instinct": True,
                }
            )

        body = _decode_payload(out)
        assert body["id"] == "run-1"
        assert body["status"] == "complete"
        mock.assert_awaited_once_with(
            name="renewal",
            custom_scenario_id="s1",
            route_to_instinct=True,
            precedent_seed=None,
        )

    @pytest.mark.asyncio
    async def test_missing_custom_scenario_id_is_rejected_locally(self) -> None:
        """The save-first contract is enforced at the handler — the run
        call never reaches the cloud without a saved id."""
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        mock = AsyncMock()
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.run_scenario_for_agent",
            new=mock,
        ):
            out = await foresight_mcp._run_scenario_handler({"name": "renewal"})

        assert out.get("is_error") is True
        assert "custom_scenario_id" in out["content"][0]["text"]
        mock.assert_not_awaited()


class TestListRunsHandler:
    @pytest.mark.asyncio
    async def test_delegates_with_pagination(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_runs_for_agent",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "items": [{"id": "r1", "status": "complete"}],
                    "limit": 5,
                    "offset": 0,
                }
            ),
        ) as mock:
            out = await foresight_mcp._list_runs_handler({"limit": 5})

        body = _decode_payload(out)
        assert body["items"][0]["id"] == "r1"
        mock.assert_awaited_once_with(limit=5, offset=0)


class TestGetRunHandler:
    @pytest.mark.asyncio
    async def test_requires_run_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        out = await foresight_mcp._get_run_handler({})
        assert out.get("is_error") is True

    @pytest.mark.asyncio
    async def test_delegates_to_agent_context(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_run_for_agent",
            new=AsyncMock(
                return_value={"ok": True, "id": "r1", "status": "complete", "result": {}}
            ),
        ):
            out = await foresight_mcp._get_run_handler({"run_id": "r1"})

        body = _decode_payload(out)
        assert body["id"] == "r1"


# ---------------------------------------------------------------------------
# Read-tool handlers — 2026-05-28 follow-up to PR #1266
# ---------------------------------------------------------------------------


class TestListProjectedDecisionsHandler:
    @pytest.mark.asyncio
    async def test_happy_path_delegates_with_args(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "items": [{"id": "pd1", "anchor_id": "rollout:training"}],
            "total": 1,
            "limit": 50,
            "offset": 0,
            "has_more": False,
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_projected_decisions_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._list_projected_decisions_handler(
                {
                    "run_id": "r1",
                    "anchor_id": "rollout:training",
                    "limit": 50,
                    "offset": 0,
                }
            )

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["items"][0]["id"] == "pd1"
        mock.assert_awaited_once_with(
            "r1", anchor_id="rollout:training", limit=50, offset=0
        )

    @pytest.mark.asyncio
    async def test_missing_run_id_is_rejected_locally(self) -> None:
        """The run_id validation runs before the agent_context call so a
        missing field never reaches the cloud — same guard pattern the
        existing get_run handler uses."""
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        mock = AsyncMock()
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_projected_decisions_for_agent",
            new=mock,
        ):
            out = await foresight_mcp._list_projected_decisions_handler({})

        assert out.get("is_error") is True
        assert "run_id" in out["content"][0]["text"]
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_projected_decisions_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._list_projected_decisions_handler({"run_id": "r1"})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]


class TestGetAggregateHandler:
    @pytest.mark.asyncio
    async def test_happy_path_passes_window(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "window_days": 7,
            "generated_at": "2026-05-28T12:00:00Z",
            "rolling_accuracy": {"points": []},
            "confidence_drift": {"points": []},
            "modal_outcome_distribution": {"entries": []},
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_aggregate_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._get_aggregate_handler({"window_days": 7})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["window_days"] == 7
        mock.assert_awaited_once_with(window_days=7)

    @pytest.mark.asyncio
    async def test_default_window_when_omitted(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_aggregate_for_agent",
            new=AsyncMock(return_value={"ok": True, "window_days": 30}),
        ) as mock:
            out = await foresight_mcp._get_aggregate_handler({})

        assert not out.get("is_error")
        mock.assert_awaited_once_with(window_days=None)

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_aggregate_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._get_aggregate_handler({})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]


class TestGetInsightsHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_items(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "items": [
                {
                    "id": "i1",
                    "kind": "accuracy_drop",
                    "title": "Accuracy dropped",
                    "body": "...",
                    "severity": "warning",
                    "anchor_refs": [],
                    "generated_at": "2026-05-28T12:00:00Z",
                }
            ],
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_insights_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._get_insights_handler({})

        body = _decode_payload(out)
        assert body["items"][0]["severity"] == "warning"
        mock.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_insights_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._get_insights_handler({})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# Backtest-read handlers — 2026-05-28 follow-up #2 (read-only per
# RFC 08 §13.1; no create_backtest tool exists).
# ---------------------------------------------------------------------------


class TestListBacktestsHandler:
    @pytest.mark.asyncio
    async def test_delegates_with_pagination(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "items": [{"id": "bt1", "scenario_name": "q3-backtest", "status": "complete"}],
            "limit": 5,
            "offset": 0,
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_backtests_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._list_backtests_handler({"limit": 5, "offset": 0})

        assert not out.get("is_error")
        body = _decode_payload(out)
        assert body["items"][0]["id"] == "bt1"
        mock.assert_awaited_once_with(limit=5, offset=0)

    @pytest.mark.asyncio
    async def test_default_args_when_omitted(self) -> None:
        """No args → default limit=10, offset=0 — mirrors list_runs."""
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_backtests_for_agent",
            new=AsyncMock(return_value={"ok": True, "items": [], "limit": 10, "offset": 0}),
        ) as mock:
            out = await foresight_mcp._list_backtests_handler({})

        assert not out.get("is_error")
        mock.assert_awaited_once_with(limit=10, offset=0)

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.list_backtests_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._list_backtests_handler({})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]


class TestGetBacktestHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_backtest(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "id": "bt1",
            "scenario_name": "q3-backtest",
            "status": "complete",
            "threshold": 0.65,
            "gate_decision": {"passed": True, "observed": 0.82},
            "result": {"calibration_summary": {}},
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_backtest_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._get_backtest_handler({"backtest_id": "bt1"})

        body = _decode_payload(out)
        assert body["id"] == "bt1"
        assert body["gate_decision"]["passed"] is True
        mock.assert_awaited_once_with("bt1")

    @pytest.mark.asyncio
    async def test_missing_backtest_id_is_rejected_locally(self) -> None:
        """Arg validation runs before the agent_context call so a missing
        backtest_id never reaches the cloud — same guard pattern as
        get_run + list_projected_decisions."""
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        mock = AsyncMock()
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_backtest_for_agent",
            new=mock,
        ):
            out = await foresight_mcp._get_backtest_handler({})

        assert out.get("is_error") is True
        assert "backtest_id" in out["content"][0]["text"]
        mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_backtest_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._get_backtest_handler({"backtest_id": "bt1"})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]


class TestGetOnboardingGateHandler:
    @pytest.mark.asyncio
    async def test_happy_path_returns_gate_state(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {
            "ok": True,
            "workspace_id": "w1",
            "unlocked": False,
            "threshold": 0.65,
            "reason": "no_backtest",
            "last_backtest_id": None,
        }
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_onboarding_gate_for_agent",
            new=AsyncMock(return_value=fake),
        ) as mock:
            out = await foresight_mcp._get_onboarding_gate_handler({})

        body = _decode_payload(out)
        assert body["reason"] == "no_backtest"
        assert body["unlocked"] is False
        # No-arg call — the gate read takes no parameters.
        mock.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_missing_workspace_surfaces_as_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import foresight as foresight_mcp

        fake = {"ok": False, "error": "no_workspace_context", "message": "stream missing"}
        with patch(
            "pocketpaw_ee.cloud.foresight.agent_context.get_onboarding_gate_for_agent",
            new=AsyncMock(return_value=fake),
        ):
            out = await foresight_mcp._get_onboarding_gate_handler({})

        assert out.get("is_error") is True
        assert "no_workspace_context" in out["content"][0]["text"]
