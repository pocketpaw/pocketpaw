"""Dispatch behavior tests for the cloud agent bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_dispatch_agent_responses_runs_agents_sequentially() -> None:
    """Eligible agents should run one-by-one, not concurrently."""
    from ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1", "user-2"],
        agents=[
            SimpleNamespace(agent_id="agent-a", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-b", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-c", respond_mode="auto"),
        ],
    )
    payload = {
        "group_id": "group-1",
        "sender_id": "user-1",
        "content": "@agent-a @agent-b @agent-c please collaborate",
        "mentions": [{"type": "agent", "id": "agent-a"}],
        "workspace_id": "ws-1",
        "attachments": [{"name": "notes.txt"}],
    }

    active = 0
    max_active = 0
    run_order: list[tuple[str, str]] = []

    async def fake_run_agent_response(
        *,
        agent_id: str,
        group_id: str,
        workspace_id: str,
        user_message: str,
        group_members: list[str],
        attachments: list[dict] | None = None,
        response_label: str | None = None,
    ) -> None:
        nonlocal active, max_active
        run_order.append(("start", agent_id))
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        run_order.append(("end", agent_id))
        assert group_id == "group-1"
        assert workspace_id == "ws-1"
        assert user_message == payload["content"]
        assert group_members == group.members
        assert attachments == payload["attachments"]
        assert response_label is None

    with (
        patch("ee.cloud.chat.group_service.get_for_dispatch", new=AsyncMock(return_value=group)),
        patch(
            "ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("ee.cloud.shared.agent_bridge._run_agent_response", new=fake_run_agent_response),
    ):
        await agent_bridge._dispatch_agent_responses(payload)

    assert max_active == 1
    assert run_order == [
        ("start", "agent-a"),
        ("end", "agent-a"),
        ("start", "agent-b"),
        ("end", "agent-b"),
        ("start", "agent-c"),
        ("end", "agent-c"),
    ]


@pytest.mark.asyncio
async def test_dispatch_agent_responses_skips_non_eligible_agents() -> None:
    """Only agents with should-respond=True are executed."""
    from ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[
            SimpleNamespace(agent_id="agent-a", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-b", respond_mode="mention_only"),
            SimpleNamespace(agent_id="agent-c", respond_mode="smart"),
        ],
    )
    run_mock = AsyncMock(return_value=None)
    should_mock = AsyncMock(side_effect=[True, False, True])

    with (
        patch("ee.cloud.chat.group_service.get_for_dispatch", new=AsyncMock(return_value=group)),
        patch("ee.cloud.shared.agent_bridge._should_agent_respond", new=should_mock),
        patch("ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-2",
                "sender_id": "user-1",
                "content": "hello",
                "mentions": [],
                "workspace_id": "ws-2",
            }
        )

    dispatched_agent_ids = [call.kwargs["agent_id"] for call in run_mock.await_args_list]
    assert dispatched_agent_ids == ["agent-a", "agent-c"]


@pytest.mark.asyncio
async def test_dispatch_agent_responses_adds_final_collaboration_reply() -> None:
    """When multiple agents respond, bridge should request one synthesized final answer."""
    from ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[
            SimpleNamespace(agent_id="agent-a", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-b", respond_mode="auto"),
        ],
    )
    run_mock = AsyncMock(side_effect=["draft from b", "draft from a", "final synthesis"])

    with (
        patch("ee.cloud.chat.group_service.get_for_dispatch", new=AsyncMock(return_value=group)),
        patch(
            "ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-3",
                "sender_id": "user-1",
                "content": "@agent-b @agent-a prepare one answer",
                "mentions": [
                    {"type": "agent", "id": "agent-b"},
                    {"type": "agent", "id": "agent-a"},
                ],
                "workspace_id": "ws-3",
            }
        )

    dispatched_agent_ids = [call.kwargs["agent_id"] for call in run_mock.await_args_list]
    assert dispatched_agent_ids == ["agent-b", "agent-a", "agent-a"]
    final_prompt = run_mock.await_args_list[-1].kwargs["user_message"]
    final_label = run_mock.await_args_list[-1].kwargs["response_label"]
    assert "Original user message:" in final_prompt
    assert "Agent agent-b:" in final_prompt
    assert "Agent agent-a:" in final_prompt
    assert final_label == "Final response:"


@pytest.mark.asyncio
async def test_dispatch_agent_responses_continues_after_agent_failure() -> None:
    """A failing agent should not block later agents in sequential mode.

    Three agents: A fails, B+C succeed. The synthesis pass runs because
    2 agents responded — the >=2-survivor condition is met."""
    from ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[
            SimpleNamespace(agent_id="agent-a", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-b", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-c", respond_mode="auto"),
        ],
    )
    run_mock = AsyncMock(
        side_effect=[
            RuntimeError("boom"),
            "draft from b",
            "draft from c",
            "final synthesis from c",
        ]
    )

    with (
        patch("ee.cloud.chat.group_service.get_for_dispatch", new=AsyncMock(return_value=group)),
        patch(
            "ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-4",
                "sender_id": "user-1",
                "content": "@agent-a @agent-b @agent-c",
                "mentions": [
                    {"type": "agent", "id": "agent-a"},
                    {"type": "agent", "id": "agent-b"},
                    {"type": "agent", "id": "agent-c"},
                ],
                "workspace_id": "ws-4",
            }
        )

    dispatched_agent_ids = [call.kwargs["agent_id"] for call in run_mock.await_args_list]
    assert dispatched_agent_ids == ["agent-a", "agent-b", "agent-c", "agent-c"]
    final_prompt = run_mock.await_args_list[-1].kwargs["user_message"]
    final_label = run_mock.await_args_list[-1].kwargs["response_label"]
    assert "Agents that could not produce a full response:" in final_prompt
    assert "agent-a" in final_prompt
    assert final_label == "Final response:"


async def test_dispatch_agent_responses_skips_synthesis_when_only_one_agent_responds() -> None:
    """When N=2 agents are dispatched and exactly one fails, the surviving
    agent must NOT synthesize its own output. Otherwise the user sees a
    redundant 'Final response:' duplicate of the lone agent's draft.

    Regression test for the synthesis-guard bug: previously the guard was
    `if len(agents_to_run) < 2 or not responses_by_agent` — passed when
    one agent survived, triggering self-synthesis."""
    from ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[
            SimpleNamespace(agent_id="agent-a", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-b", respond_mode="auto"),
        ],
    )
    # A fails, B succeeds, no third call expected because synthesis must skip.
    run_mock = AsyncMock(side_effect=[RuntimeError("boom"), "draft from b"])

    with (
        patch("ee.cloud.chat.group_service.get_for_dispatch", new=AsyncMock(return_value=group)),
        patch(
            "ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
    ):
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-5",
                "sender_id": "user-1",
                "content": "@agent-a @agent-b",
                "mentions": [
                    {"type": "agent", "id": "agent-a"},
                    {"type": "agent", "id": "agent-b"},
                ],
                "workspace_id": "ws-5",
            }
        )

    # Only two calls: A (fails) and B (succeeds). No synthesis call from B.
    assert run_mock.await_count == 2
    dispatched_agent_ids = [call.kwargs["agent_id"] for call in run_mock.await_args_list]
    assert dispatched_agent_ids == ["agent-a", "agent-b"]
    # No "Final response:" label was emitted because synthesis was skipped.
    final_labels = [call.kwargs.get("response_label") for call in run_mock.await_args_list]
    assert "Final response:" not in final_labels
