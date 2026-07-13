# tests/cloud/shared/test_agent_bridge_billing.py — proves the run-start billing
# gate in agent_bridge._dispatch_agent_responses rejects an over-budget workspace
# ABOVE the _smart_relevance_check Haiku pre-classifier (reached via
# _should_agent_respond) AND above _run_agent_response/pool.run, so NO model call
# fires on the group/DM auto-response path. This is the regression guard for the
# money-leak correction: gating only before pool.run would still let an over-budget
# tenant spend on the relevance pre-classifier.
#
# Created 2026-07-08 (feat/billing-enforce-gate).
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_dispatch_rejects_over_budget_before_any_model_call() -> None:
    """Over-budget workspace: neither the Haiku pre-classifier
    (_should_agent_respond) nor the agent run (_run_agent_response) is reached,
    and exactly one terminal AgentError is emitted to the group."""
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits
    from pocketpaw_ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[SimpleNamespace(agent_id="agent-a", respond_mode="smart")],
    )
    should_mock = AsyncMock(return_value=True)
    run_mock = AsyncMock(return_value=None)
    emit_mock = AsyncMock()

    with (
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.credits.guards.over_billing_limit",
            new=AsyncMock(return_value=InsufficientCredits(requested=1, available=0)),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond", new=should_mock),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=emit_mock),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-over",
                "sender_id": "user-1",
                "content": "hello",
                "mentions": [],
                "workspace_id": "ws-over",
            }
        )

    # The pre-classifier and the agent run must NOT be reached — no model call.
    should_mock.assert_not_awaited()
    run_mock.assert_not_awaited()
    # Exactly one terminal AgentError emitted to the group.
    assert emit_mock.await_count == 1
    emitted = emit_mock.await_args_list[0].args[0]
    assert emitted.data["code"] == "credits.insufficient"
    assert emitted.data["group_id"] == "group-over"


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_within_budget() -> None:
    """Within budget (guard returns None): normal dispatch proceeds and the agent
    is run — the gate is a no-op when the workspace is funded."""
    from pocketpaw_ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[SimpleNamespace(agent_id="agent-a", respond_mode="auto")],
    )
    run_mock = AsyncMock(return_value="ok")

    with (
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.credits.guards.over_billing_limit",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-ok",
                "sender_id": "user-1",
                "content": "hello",
                "mentions": [],
                "workspace_id": "ws-ok",
            }
        )

    run_mock.assert_awaited()
