# tests/cloud/shared/test_agent_bridge_group_history.py
# Pins the history the group/DM bridge hands ``pool.run``.
#
# ``message_service.send_message`` inserts the user's message BEFORE it emits
# ``message.sent``, so ``list_recent_for_group`` already returns the message that
# triggered this run. Passing it as the last history line AND as the prompt
# showed the model the same message twice. And only THIS agent's own replies are
# the assistant's turns: another agent's reply labelled ``assistant`` reads as
# something this agent said, and unnamed human lines make a group of people look
# like one user.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _msg(mid, content, *, sender=None, sender_type="user", agent=None, sender_name=None):
    return SimpleNamespace(
        id=mid,
        content=content,
        sender=sender,
        sender_type=sender_type,
        agent=agent,
        sender_name=sender_name,
    )


@pytest.mark.asyncio
async def test_group_history_drops_the_trigger_and_names_other_speakers() -> None:
    from pocketpaw_ee.cloud.shared import agent_bridge

    recent = [
        _msg("m1", "Let's plan the launch", sender="u-alice"),
        _msg(
            "m2",
            "I drafted the copy",
            sender_type="agent",
            agent="agent-other",
            sender_name="Scribe",
        ),
        _msg(
            "m3",
            "I will check the budget",
            sender_type="agent",
            agent="agent-me",
            sender_name="Paw",
        ),
        _msg("m4", "What do you think?", sender="u-bob"),
    ]
    seen: dict = {}

    async def fake_run(agent_id, user_message, session_key, history, **kwargs):
        seen["history"] = history
        seen["message"] = user_message
        yield SimpleNamespace(type="message", content="ok")
        yield SimpleNamespace(type="done", content="")

    pool = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(agent_name="Paw")),
        run=fake_run,
        observe=AsyncMock(return_value=None),
    )

    with (
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.list_recent_for_group",
            new=AsyncMock(return_value=recent),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(return_value=SimpleNamespace(id="m9")),
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "pocketpaw_ee.cloud.auth.service.resolve_display_names",
            new=AsyncMock(return_value={"u-alice": "Alice", "u-bob": "Bob"}),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-me",
            group_id="g1",
            workspace_id="ws1",
            user_message="What do you think?",
            group_members=["u-alice", "u-bob"],
            trigger_message_id="m4",
        )

    history = seen["history"]
    contents = [h["content"] for h in history]
    assert not any("What do you think?" in c for c in contents), (
        "the triggering message is the prompt; it must not also be the last history line"
    )
    by_text = {h["content"]: h["role"] for h in history}
    assert by_text["I will check the budget"] == "assistant", "this agent's own reply"
    other = next(h for h in history if "I drafted the copy" in h["content"])
    assert other["role"] == "user", "another agent's reply is not this agent's turn"
    assert other["content"].startswith("Scribe"), other["content"]
    alice = next(h for h in history if "plan the launch" in h["content"])
    assert alice["role"] == "user" and alice["content"].startswith("Alice"), alice["content"]
