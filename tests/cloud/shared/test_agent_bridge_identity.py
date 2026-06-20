"""Regression test: the group/DM agent bridge binds workspace/user identity
around ``pool.run`` so in-process MCP tools that read scope from ContextVars
(fabric, instinct, decisions, connectors) can reach the store.

Before the fix, ``_run_agent_response`` called ``pool.run`` directly without
``attach_agent_identity``, so ``current_workspace_id()`` was ``None`` during
the run and every ContextVar-scoped tool returned "requires workspace context
(call from a cloud chat session)". The SSE chat path set identity in
``run_core``; this path did not.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_agent_response_sets_workspace_contextvar_during_run() -> None:
    """During ``pool.run``, the workspace/user ContextVars read by in-process
    MCP tools must resolve to the dispatched agent's workspace and id."""
    from pocketpaw_ee.cloud.chat.agent_service import (
        current_user_id,
        current_workspace_id,
    )
    from pocketpaw_ee.cloud.shared import agent_bridge

    seen: dict[str, str | None] = {}

    async def fake_run(agent_id, user_message, session_key, history, **kwargs):
        # Captured at the moment a tool would observe the ContextVars.
        seen["workspace_id"] = current_workspace_id()
        seen["user_id"] = current_user_id()
        # Yield a minimal terminal stream so the function completes.
        yield SimpleNamespace(type="message", content="ok")
        yield SimpleNamespace(type="done", content="")

    pool = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(agent_name="PocketPaw")),
        run=fake_run,
        observe=AsyncMock(return_value=None),
    )

    with (
        patch(
            "pocketpaw.agents.pool.get_agent_pool",
            return_value=pool,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.list_recent_for_group",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(return_value=SimpleNamespace(id="m1")),
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-x",
            group_id="group-x",
            workspace_id="ws-x",
            user_message="How many Application objects are in Fabric, by status?",
            group_members=["user-1"],
        )

    assert seen["workspace_id"] == "ws-x"
    assert seen["user_id"] == "agent-x"


@pytest.mark.asyncio
async def test_run_agent_response_resets_contextvar_after_run() -> None:
    """The identity tokens must be reset after the run so a later run in the
    same task/loop doesn't inherit a stale workspace."""
    from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id
    from pocketpaw_ee.cloud.shared import agent_bridge

    async def fake_run(agent_id, user_message, session_key, history, **kwargs):
        yield SimpleNamespace(type="message", content="ok")
        yield SimpleNamespace(type="done", content="")

    pool = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(agent_name="PocketPaw")),
        run=fake_run,
        observe=AsyncMock(return_value=None),
    )

    assert current_workspace_id() is None

    with (
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.list_recent_for_group",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(return_value=SimpleNamespace(id="m1")),
        ),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-y",
            group_id="group-y",
            workspace_id="ws-y",
            user_message="hi",
            group_members=["user-1"],
        )

    # Reset after the run — no leak into the surrounding context.
    assert current_workspace_id() is None
