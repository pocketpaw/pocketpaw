"""Smoke test: 200 OK SSE stream with every required event kind."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as mod


class _Pool:
    def __init__(self):
        self.observed = []

    async def get(self, aid):
        return SimpleNamespace(agent_id=aid, agent_name="A")

    async def run(self, aid, message, session_key, history=None, knowledge_context=""):
        yield SimpleNamespace(type="thinking", content="t", metadata={})
        yield SimpleNamespace(type="tool_use", content={"tool": "web_fetch"}, metadata={})
        yield SimpleNamespace(type="message", content="hello ", metadata={})
        yield SimpleNamespace(type="message", content="world", metadata={})
        yield SimpleNamespace(type="done", content="", metadata={})

    async def observe(self, aid, user_input, agent_output):
        self.observed.append((aid, user_input, agent_output))


class _FakeMsg:
    id = "aid"
    createdAt = datetime.now(UTC)


@pytest.mark.asyncio
async def test_smoke_all_event_kinds(cloud_app_client: AsyncClient):
    ctx = SimpleNamespace(
        kind=SimpleNamespace(value="dm"),
        scope_id="dm1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )

    async def resolver(**_):
        return ctx

    async def persist(_, __):
        return "uid"

    async def persist_a(_, __, ___):
        return _FakeMsg()

    async def bc_new(_, __, ___, ____, *, created_at):
        return None

    async def bc_typing(_, *, active):
        return None

    pool = _Pool()
    with (
        patch.object(mod, "resolve_scope_context", resolver),
        patch.object(mod, "_persist_user_message", persist),
        patch.object(mod, "_persist_assistant_message", persist_a),
        patch.object(mod, "_broadcast_message_new", bc_new),
        patch.object(mod, "_broadcast_agent_typing", bc_typing),
        patch.object(mod, "get_agent_pool", lambda: pool),
    ):
        async with cloud_app_client.stream(
            "POST", "/cloud/chat/dm/dm1/agent", json={"content": "hi"}
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    names: list[str] = []
    for block in body.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("event: "):
                names.append(line[7:])

    assert names[0] == "message.persisted"
    assert "stream_start" in names
    assert "thinking" in names
    assert "tool_start" in names
    assert names.count("chunk") >= 2
    assert names[-1] == "stream_end"
    assert pool.observed[0][0] == "a1"
