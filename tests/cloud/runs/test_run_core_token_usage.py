# tests/cloud/runs/test_run_core_token_usage.py
# Created 2026-06-10 (sov/w3a-igw — per-run token metering). Pins the W3a fix:
# real token usage is threaded through the run instead of being dropped.
#   * _drive_agent_loop: a backend ``token_usage`` AgentEvent is surfaced as a
#     ("token_usage", metadata) tuple (was silently discarded — no handler).
#   * execute_run: the captured usage lands in BOTH the persisted run (via
#     mark_completed / mark_terminal) AND the ``stream_end`` SSE frame, replacing
#     the old hardcoded ``{}``. Covers the success, empty-text, and cancelled
#     paths.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio


# A realistic ClaudeSDKBackend token_usage payload (see
# src/pocketpaw/agents/claude_sdk.py — the ResultMessage usage branch).
_USAGE_META = {
    "input_tokens": 1200,
    "output_tokens": 350,
    "cached_input_tokens": 800,
    "total_cost_usd": 0.0123,
    "model": "claude-sonnet-4",
    "backend": "claude_agent_sdk",
}


# ---------------------------------------------------------------------------
# _drive_agent_loop surfaces the backend token_usage event
# ---------------------------------------------------------------------------


def _scope_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _drive_and_collect(monkeypatch, backend_events: list[Any]) -> list[tuple[str, dict]]:
    """Drive _drive_agent_loop against a fake pool that yields ``backend_events``
    and return the (event_name, event_data) tuples the loop produced."""

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A")

        def run(self, agent_id, content, session_key, **run_kwargs):
            async def _gen():
                for ev in backend_events:
                    yield ev

            return _gen()

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())

    async def _fake_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)

    async def _never_cancelled():
        return False

    out: list[tuple[str, dict]] = []
    gen = run_core._drive_agent_loop(
        _scope_ctx(),
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=None,
        is_cancelled=_never_cancelled,
        emit_stream_start=False,
    )
    async for ev in gen:
        out.append(ev)
    return out


async def test_drive_loop_surfaces_token_usage(monkeypatch):
    """A backend ``token_usage`` AgentEvent is yielded as a ("token_usage", meta)
    tuple — previously there was no handler so the event was dropped."""
    events = [
        SimpleNamespace(type="message", content="hi there", metadata={}),
        SimpleNamespace(type="token_usage", content="", metadata=_USAGE_META),
        SimpleNamespace(type="done", content=""),
    ]
    out = await _drive_and_collect(monkeypatch, events)

    usage_events = [data for name, data in out if name == "token_usage"]
    assert len(usage_events) == 1, f"expected one token_usage tuple, got {out}"
    assert usage_events[0]["input_tokens"] == 1200
    assert usage_events[0]["output_tokens"] == 350
    assert usage_events[0]["backend"] == "claude_agent_sdk"
    # The metadata is copied, not the live backend dict.
    assert usage_events[0] is not _USAGE_META


async def test_drive_loop_no_usage_event_when_backend_silent(monkeypatch):
    """A backend that never emits ``token_usage`` produces no usage tuple — the
    metering path is purely additive."""
    events = [
        SimpleNamespace(type="message", content="hi", metadata={}),
        SimpleNamespace(type="done", content=""),
    ]
    out = await _drive_and_collect(monkeypatch, events)
    assert [name for name, _ in out if name == "token_usage"] == []


# ---------------------------------------------------------------------------
# execute_run records the usage on the doc + the stream_end frame
# ---------------------------------------------------------------------------


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


async def test_execute_run_records_usage_on_stream_end_and_persist(monkeypatch):
    """End-to-end: a run that emits a token_usage event must (a) carry the real
    counts in the terminal ``stream_end`` frame and (b) hand the usage to the
    persist/mark-completed path — instead of the old hardcoded ``{}``."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted_usage: dict[str, Any] = {}

    async def _capture_persist(spec, ctx, full_text, attachments, usage=None):
        persisted_usage["usage"] = usage
        return "assistant-msg-1"

    async def fake_agent_events(spec, ctx):
        yield ("chunk", {"content": "Hello", "type": "text"})
        yield ("token_usage", dict(_USAGE_META))

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _capture_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    # The token_usage event is written to the live stream...
    assert "token_usage" in [e.event for e in events]
    end = events[-1]
    assert end.event == "stream_end"
    # ...and the terminal frame's usage is the REAL payload, not {}.
    assert end.data["usage"]["input_tokens"] == 1200
    assert end.data["usage"]["output_tokens"] == 350
    assert end.data["usage"]["backend"] == "claude_agent_sdk"
    # ...and the persist path received the same usage to store on the run doc.
    assert persisted_usage["usage"]["input_tokens"] == 1200


async def test_execute_run_usage_empty_when_no_token_event(monkeypatch):
    """A run with no token_usage event keeps the legacy empty usage — no
    regression for backends / turns that report nothing."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    async def _persist_stub(spec, ctx, full_text, attachments, usage=None):
        return "assistant-msg-1"

    async def fake_agent_events(spec, ctx):
        yield ("chunk", {"content": "Hello", "type": "text"})

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].data["usage"] == {}


async def test_execute_run_empty_text_path_carries_usage(monkeypatch):
    """A tool-only turn (no assistant text) that still reports usage must record
    it: empty-text completes via mark_completed, which now takes the usage."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    mark_calls: list[dict[str, Any]] = []

    async def _track_completed(run_id, *, assistant_message_id, partial_text, usage=None):
        mark_calls.append({"usage": usage})

    async def fake_agent_events(spec, ctx):
        # Tool-only: no chunk text, but the backend still reports tokens.
        yield ("tool_start", {"tool": "noop", "input": {}})
        yield ("token_usage", dict(_USAGE_META))

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_completed", _track_completed
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    end = events[-1]
    assert end.event == "stream_end"
    assert end.data["usage"]["input_tokens"] == 1200
    # The empty-text completion path forwarded the usage to mark_completed.
    assert len(mark_calls) == 1
    assert mark_calls[0]["usage"]["input_tokens"] == 1200
