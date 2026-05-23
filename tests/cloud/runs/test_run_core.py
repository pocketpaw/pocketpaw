"""Tests for ``execute_run``."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio


def _spec() -> RunSpec:
    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def _noop(*a, **k):
    return None


async def _persist_stub(spec, ctx, full_text, attachments):
    return "assistant-msg-1"


async def fake_agent_events(spec, ctx):
    yield ("chunk", {"content": "Hello", "type": "text"})
    yield ("chunk", {"content": " world", "type": "text"})


async def fake_resolve_scope_context(**_):
    class _Ctx:
        kind = type("K", (), {"value": "session"})()
        scope_id = "s1"
        workspace_id = "w1"
        user_id = "u1"
        target_agent_id = "a1"
        members = ["u1"]
        session_id = None
        intent = None

    return _Ctx()


async def test_execute_run_writes_chunks_then_stream_end(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["chunk", "chunk", "stream_end"]
    assert events[-1].data["assistant_message_id"] == "assistant-msg-1"
    assert events[-1].data["cancelled"] is False


async def test_execute_run_cancelled_does_not_persist(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    await transport.request_cancel("r1")  # cancel BEFORE the run starts

    persisted: list[str] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    # cancel + mark_terminal path also touches run_service.mark_terminal
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _noop)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].data["cancelled"] is True
    assert events[-1].data["assistant_message_id"] is None
    assert persisted == []
