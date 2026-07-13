"""Dispatch behavior tests for the cloud agent bridge.

Updated 2026-06-28 (feat/aiam-agent-revoke, AW-5): added the disabled-agent
skip test — when ``pool.get`` raises ``AgentDisabled``, ``_run_agent_response``
returns ``None`` (skips the agent cleanly) instead of erroring to the channel.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_dispatch_agent_responses_runs_agents_sequentially() -> None:
    """Eligible agents should run one-by-one, not concurrently."""
    from pocketpaw_ee.cloud.shared import agent_bridge

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
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response",
            new=fake_run_agent_response,
        ),
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
    from pocketpaw_ee.cloud.shared import agent_bridge

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
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond", new=should_mock),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
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
    from pocketpaw_ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[
            SimpleNamespace(agent_id="agent-a", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-b", respond_mode="auto"),
        ],
    )
    run_mock = AsyncMock(side_effect=["draft from b", "draft from a", "final synthesis"])

    with (
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
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
    from pocketpaw_ee.cloud.shared import agent_bridge

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
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
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
    from pocketpaw_ee.cloud.shared import agent_bridge

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
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response", new=run_mock),
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


# --- disabled-agent skip (AW-4 behavior, AW-5 regression test) -------------


@pytest.mark.asyncio
async def test_run_agent_response_skips_disabled_agent() -> None:
    """A soft-disabled agent is revoked at the AgentPool chokepoint: ``pool.get``
    raises ``AgentDisabled``. The bridge must catch it and return ``None`` —
    skip the agent with no error to the channel, no stream-start emit, no 500.

    This is the auto-dispatch half of the revoke-everywhere guarantee: a
    disabled agent simply stops responding in groups/DMs until re-enabled.
    """
    from pocketpaw_ee.cloud.shared import agent_bridge

    from pocketpaw.agents.errors import AgentDisabled

    fake_pool = SimpleNamespace(get=AsyncMock(side_effect=AgentDisabled("agent-x")))
    emit_mock = AsyncMock()

    with (
        patch(
            "pocketpaw.agents.pool.get_agent_pool",
            return_value=fake_pool,
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=emit_mock),
    ):
        result = await agent_bridge._run_agent_response(
            agent_id="agent-x",
            group_id="group-disabled",
            workspace_id="ws-disabled",
            user_message="hello?",
            group_members=["user-1"],
        )

    # Skipped cleanly — no reply text, and nothing emitted to the channel
    # (no AgentStreamStart for a revoked agent).
    assert result is None
    assert emit_mock.await_count == 0
    fake_pool.get.assert_awaited_once_with("agent-x")


@pytest.mark.asyncio
async def test_dispatch_skips_disabled_agent_keeps_live_one() -> None:
    """End-to-end at the dispatch layer: two auto-mode agents, one disabled.

    The disabled agent's ``pool.get`` raises ``AgentDisabled`` (revoked at the
    chokepoint), which ``_run_agent_response`` catches and turns into ``None``
    (proven directly in ``test_run_agent_response_skips_disabled_agent``); the
    live agent still responds. Here we model that contract — disabled -> ``None``
    — and prove auto-dispatch does NOT 500 on a disabled member and does not
    drop the healthy one.
    """
    from pocketpaw_ee.cloud.shared import agent_bridge

    group = SimpleNamespace(
        members=["user-1"],
        agents=[
            SimpleNamespace(agent_id="agent-live", respond_mode="auto"),
            SimpleNamespace(agent_id="agent-disabled", respond_mode="auto"),
        ],
    )

    async def fake_run_agent_response(*, agent_id: str, **_kwargs):
        # Mirror the real bridge contract: a disabled agent is a clean skip
        # (returns None, the bridge having caught AgentDisabled internally); a
        # live agent returns its reply text.
        if agent_id == "agent-disabled":
            return None
        return "live reply"

    with (
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=AsyncMock(return_value=group),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._should_agent_respond",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.shared.agent_bridge._run_agent_response",
            new=AsyncMock(side_effect=fake_run_agent_response),
        ) as run_mock,
    ):
        # Should not raise even though one member is disabled.
        await agent_bridge._dispatch_agent_responses(
            {
                "group_id": "group-mixed",
                "sender_id": "user-1",
                "content": "anyone home?",
                "mentions": [],
                "workspace_id": "ws-mixed",
            }
        )

    # Both were attempted (disabled isn't pre-filtered from the roster), but
    # only the live one produced a reply and the disabled one was a clean skip.
    dispatched = [call.kwargs["agent_id"] for call in run_mock.await_args_list]
    assert "agent-live" in dispatched
    assert "agent-disabled" in dispatched
