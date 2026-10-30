# tests/cloud/shared/test_agent_bridge_steps.py
# Created 2026-09-28 (feat/persist-tool-steps). The group/DM bridge streams the
# same backend events the chat run does, so its replies persist the same
# ``steps``: thinking merged into one block, the claude_sdk provisional
# announcement folded into its real call, results paired by tool name. The
# first test drives ``_run_agent_response`` with the harness the other bridge
# tests use and checks what reaches ``create_agent_message``; the second checks
# ``create_agent_message`` stores the steps and the group wire mapper returns them.
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from beanie import PydanticObjectId


def _ev(type_: str, content="", metadata=None):
    return SimpleNamespace(type=type_, content=content, metadata=metadata or {})


async def _drive(events) -> dict:
    from pocketpaw_ee.cloud.models.message import Message as _RealMessage
    from pocketpaw_ee.cloud.shared import agent_bridge

    pool = MagicMock()
    pool.get = AsyncMock(return_value=SimpleNamespace(agent_name="Test Agent", backend=None))
    pool.observe = AsyncMock()

    async def fake_run(*_a, **_k):
        for event in events:
            yield event

    pool.run = fake_run

    limit_mock = MagicMock()
    limit_mock.to_list = AsyncMock(return_value=[])
    sort_mock = MagicMock()
    sort_mock.limit = MagicMock(return_value=limit_mock)
    find_mock = MagicMock()
    find_mock.sort = MagicMock(return_value=sort_mock)

    created: dict = {}

    async def fake_create_agent_message(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(id="msg-1")

    with (
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
        patch.multiple(
            _RealMessage, create=True, group=MagicMock(), deleted=MagicMock(), createdAt=MagicMock()
        ),
        patch.object(_RealMessage, "find", MagicMock(return_value=find_mock)),
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(side_effect=fake_create_agent_message),
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-1",
            group_id="group-1",
            workspace_id="ws-1",
            user_message="latest release?",
            group_members=["user-1"],
        )
    return created


@pytest.mark.asyncio
async def test_a_bridge_reply_persists_its_steps():
    created = await _drive(
        [
            _ev("thinking", "Looking "),
            _ev("thinking", "it up."),
            _ev(
                "tool_use",
                "Using web_search...",
                {"name": "web_search", "input": {}, "input_pending": True},
            ),
            _ev(
                "tool_use",
                "Using web_search...",
                {"name": "web_search", "input": {"query": "pocketpaw"}},
            ),
            _ev("tool_result", "v0.4.18", {"name": "web_search"}),
            _ev("tool_use", "Using deploy...", {"name": "deploy", "input": {"env": "prod"}}),
            _ev("message", "It is v0.4.18."),
            _ev("done"),
        ]
    )

    steps = created["steps"]
    assert [(s["kind"], s["tool"], s["status"]) for s in steps] == [
        ("thinking", "", "complete"),
        ("tool", "web_search", "complete"),
        ("tool", "deploy", "missing_result"),
    ]
    assert steps[0]["text"] == "Looking it up."
    assert steps[1]["input"] == {"query": "pocketpaw"}
    assert steps[1]["output"] == "v0.4.18"
    assert created["steps_omitted"] == 0


@pytest.mark.asyncio
async def test_a_plain_bridge_reply_passes_no_step_kwargs():
    created = await _drive([_ev("message", "Hello."), _ev("done")])
    assert "steps" not in created and "steps_omitted" not in created


@pytest.mark.asyncio
async def test_create_agent_message_stores_steps_for_the_wire(mongo_db):  # noqa: ARG001
    from pocketpaw_ee.cloud.chat import message_service

    rec_steps = [
        {
            "id": "s1",
            "kind": "tool",
            "tool": "grep",
            "input": {"pattern": "x"},
            "output": "1 hit",
            "status": "complete",
        }
    ]
    msg = await message_service.create_agent_message(
        group_id=str(PydanticObjectId()),
        agent_id="agent-1",
        content="Found it.",
        steps=rec_steps,
        steps_omitted=3,
    )
    assert [s.tool for s in msg.steps] == ["grep"]
    wire = message_service._message_response(msg)
    assert wire["steps"][0]["input"] == {"pattern": "x"}
    assert wire["stepsOmitted"] == 3
    domain = message_service._message_doc_to_domain(msg)
    assert domain.steps[0].output == "1 hit"
