"""Wires AgentPool + MessageService into the router; verifies soul routing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from ee.cloud.chat import agent_router as mod


class _FakePool:
    """Mimics AgentPool for tests: scripts a run and records observe calls."""

    def __init__(self):
        self.observed: list[tuple[str, str, str]] = []
        self.knowledge_context_seen: str = ""

    async def get(self, agent_id):
        return SimpleNamespace(
            agent_id=agent_id,
            agent_name="Agent " + agent_id,
            config={"backend": "claude_agent_sdk"},
        )

    async def run(
        self,
        agent_id,
        message,
        session_key,
        history=None,
        knowledge_context="",
        instructions="",
    ):
        self.knowledge_context_seen = knowledge_context
        self.instructions_seen = instructions
        yield SimpleNamespace(type="thinking", content="pondering", metadata={})
        yield SimpleNamespace(type="tool_use", content={"tool": "web_fetch"}, metadata={})
        yield SimpleNamespace(type="message", content="Here is your answer.", metadata={})
        yield SimpleNamespace(
            type="message",
            content='\n\n```json\n{"lifecycle":"v1","widgets":[]}\n```',
            metadata={},
        )
        yield SimpleNamespace(type="done", content="", metadata={})

    async def observe(self, agent_id, user_input, agent_output):
        self.observed.append((agent_id, user_input, agent_output))


@pytest.mark.asyncio
async def test_full_run_emits_chunks_ripple_and_routes_observe_to_target(
    cloud_app_client: AsyncClient,
):
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="agent_X",
        agent_ids_in_scope=["agent_X"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )
    fake_pool = _FakePool()

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id_1"

    class _FakeMsg:
        id = "assistant_msg_id_1"
        createdAt = __import__("datetime").datetime.now(__import__("datetime").UTC)

    async def fake_persist_assistant(ctx, content, attachments):
        return _FakeMsg()

    async def fake_broadcast_new(ctx, message_id, content, attachments, created_at):
        return None

    async def fake_broadcast_typing(ctx, active):
        return None

    async def fake_build_knowledge_context(_ctx, *, user_message, attachments=None, mentions=None):
        return "<scope>group g1</scope>\n\n<knowledge-base>ctx</knowledge-base>"

    with (
        patch.object(mod, "resolve_scope_context", fake_resolver),
        patch.object(mod, "_persist_user_message", fake_persist),
        patch.object(mod, "_persist_assistant_message", fake_persist_assistant),
        patch.object(mod, "_broadcast_message_new", fake_broadcast_new),
        patch.object(mod, "_broadcast_agent_typing", fake_broadcast_typing),
        patch.object(mod, "build_knowledge_context", fake_build_knowledge_context),
        patch.object(mod, "get_agent_pool", lambda: fake_pool),
    ):
        async with cloud_app_client.stream(
            "POST",
            "/cloud/chat/group/g1/agent",
            json={"content": "hi"},
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    names: list[str] = []
    payloads: list[dict] = []
    for block in body.strip().split("\n\n"):
        name = None
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            names.append(name)
            payloads.append(json.loads(data) if data else {})

    assert names[0] == "message.persisted"
    assert "stream_start" in names
    assert "thinking" in names
    assert "tool_start" in names
    assert names.count("chunk") >= 1
    assert "ripple" in names
    assert names[-1] == "stream_end"
    end_payload = payloads[names.index("stream_end")]
    assert end_payload["assistant_message_id"] == "assistant_msg_id_1"
    assert end_payload["cancelled"] is False

    # Soul routed to the target agent, not the global PocketPaw soul.
    assert fake_pool.observed and fake_pool.observed[0][0] == "agent_X"
    assert "<knowledge-base>ctx</knowledge-base>" in fake_pool.knowledge_context_seen
