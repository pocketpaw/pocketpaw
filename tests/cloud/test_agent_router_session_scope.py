"""Unit tests for _ensure_scope_session with SESSION kind.

Tests the SESSION scope kind handling in _ensure_scope_session, which
should fetch an existing Session and return its sessionId without creating.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as mod
from pocketpaw_ee.cloud.chat.agent_router import _ensure_scope_session
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind


def _ctx():
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="000000000000000000000001",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )


@pytest.mark.asyncio
async def test_ensure_scope_session_returns_existing_session_id():
    from pocketpaw_ee.cloud.models.session import Session

    fake = Session.model_construct(sessionId="websocket_abc123")
    with patch.object(Session, "get", AsyncMock(return_value=fake)):
        sid = await _ensure_scope_session(_ctx())
    assert sid == "websocket_abc123"


@pytest.mark.asyncio
async def test_ensure_scope_session_returns_none_when_missing():
    from pocketpaw_ee.cloud.models.session import Session

    with patch.object(Session, "get", AsyncMock(return_value=None)):
        sid = await _ensure_scope_session(_ctx())
    assert sid is None


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
async def test_smoke_session_scope_all_event_kinds(cloud_app_client: AsyncClient):
    ctx = SimpleNamespace(
        kind=SimpleNamespace(value="session"),
        scope_id="session-id-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
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
            "POST", "/cloud/chat/session/session-id-1/agent", json={"content": "hi"}
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


@pytest.mark.asyncio
async def test_cancel_session_scope(cloud_app_client: AsyncClient):
    """A second POST for the same (scope, scope_id, user_id) triggers cancel
    on the first by setting its cancel event."""
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="session"),
        scope_id="session-id-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id"

    first_done = asyncio.Event()

    async def slow_stream(ctx, user_msg_id, body, cancel_event, *, history=None):
        yield (
            "stream_start",
            {"run_id": "r", "agent_id": "a1", "scope": "session", "scope_id": "session-id-1"},
        )
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=5.0)
        finally:
            first_done.set()
        yield ("stream_end", {"assistant_message_id": None, "usage": {}, "cancelled": True})

    async def fast_stream(ctx, user_msg_id, body, cancel_event, *, history=None):
        yield ("stream_end", {"assistant_message_id": "m", "usage": {}, "cancelled": False})

    with (
        patch.object(mod, "resolve_scope_context", fake_resolver),
        patch.object(mod, "_persist_user_message", fake_persist),
    ):
        with patch.object(mod, "_run_agent_stream", slow_stream):
            first_task = asyncio.create_task(
                cloud_app_client.post(
                    "/cloud/chat/session/session-id-1/agent",
                    json={"content": "1"},
                )
            )
            # Give the first request time to register its cancel_event
            await asyncio.sleep(0.05)
        with patch.object(mod, "_run_agent_stream", fast_stream):
            second = await cloud_app_client.post(
                "/cloud/chat/session/session-id-1/agent",
                json={"content": "2"},
            )
        assert second.status_code == 200
        await asyncio.wait_for(first_task, timeout=2.0)
        assert first_done.is_set()
