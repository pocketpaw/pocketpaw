"""Tests for ``execute_run``."""

from __future__ import annotations

import asyncio
from typing import Any

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


async def fake_agent_events_empty(spec, ctx):
    # Tool-only turn: the agent runs to completion but produces no text.
    yield ("tool_start", {"tool": "noop", "input": {}})
    yield ("tool_result", {"tool": "noop", "output": {}})


async def test_execute_run_empty_text_marks_completed(monkeypatch):
    """Regression: a non-cancelled run with no assistant text must still
    flip the ChatRunDoc out of ``running`` — without this, the sweeper
    eventually marks it ``interrupted`` (semantically wrong) and until
    then ``active_run`` ghosts on the client."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_completed(run_id, *, assistant_message_id, partial_text):
        mark_calls.append(
            {
                "fn": "mark_completed",
                "run_id": run_id,
                "assistant_message_id": assistant_message_id,
                "partial_text": partial_text,
            }
        )

    async def _track_terminal(run_id, *, status, partial_text="", **k):
        mark_calls.append(
            {
                "fn": "mark_terminal",
                "run_id": run_id,
                "status": status,
                "partial_text": partial_text,
            }
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_empty)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_completed", _track_completed
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].data["cancelled"] is False
    assert events[-1].data["assistant_message_id"] is None
    assert persisted == []
    assert mark_calls == [
        {
            "fn": "mark_completed",
            "run_id": "r1",
            "assistant_message_id": None,
            "partial_text": "",
        }
    ]


async def fake_agent_events_backend_error(spec, ctx):
    # Backend-yielded error (e.g. codex_cli without ``openai-codex-sdk``),
    # surfaced through _drive_agent_loop's ``elif etype == "error"`` branch.
    yield ("error", {"code": "agent.backend_error", "message": "codex sdk missing"})


async def test_execute_run_backend_error_marks_failed(monkeypatch):
    """Regression for PR #1191's fix, ported into _drive_agent_loop: when
    the backend yields an error event, the doc must end up ``failed`` (not
    silently ``completed`` via the empty-text path).

    Replaces the wire-shape test ``tests/cloud/test_agent_router_backend_error.py``
    that PR #1191 added against the now-deleted ``_run_agent_stream``.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_terminal(run_id, *, status, partial_text="", error=None, **k):
        mark_calls.append(
            {"run_id": run_id, "status": status, "partial_text": partial_text, "error": error}
        )

    async def _track_completed(*a, **k):
        mark_calls.append({"fn": "mark_completed"})

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_backend_error)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_completed", _track_completed
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    # ``error`` is terminal — read_events stops here, and we MUST NOT have
    # appended a stream_end frame after it.
    assert [e.event for e in events] == ["error"]
    assert events[0].data["message"] == "codex sdk missing"
    assert persisted == []
    assert mark_calls == [
        {
            "run_id": "r1",
            "status": "failed",
            "partial_text": "",
            "error": "codex sdk missing",
        }
    ]


async def fake_agent_events_cancelled(spec, ctx):
    yield ("chunk", {"content": "partial ", "type": "text"})
    raise asyncio.CancelledError()


async def test_execute_run_propagates_cancellation(monkeypatch):
    """When the task is cancelled mid-stream (arq worker shutdown), the
    agent loop must (a) NOT swallow CancelledError, (b) mark the run
    ``interrupted`` with the partial text preserved, (c) append a terminal
    event to the stream so live SSE subscribers finalise, and (d) re-raise
    so the arq worker actually exits."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_terminal(run_id, *, status, partial_text="", error=None, **k):
        mark_calls.append(
            {"run_id": run_id, "status": status, "partial_text": partial_text, "error": error}
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_cancelled)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    with pytest.raises(asyncio.CancelledError):
        await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    # The chunk made it through, then a terminal `interrupted` frame.
    assert events[0].event == "chunk"
    assert events[-1].event == "interrupted"
    assert events[-1].is_terminal
    assert persisted == []
    assert mark_calls == [
        {
            "run_id": "r1",
            "status": "interrupted",
            "partial_text": "partial ",
            "error": None,
        }
    ]


async def test_execute_run_cancellation_preserves_original_exception(monkeypatch):
    """Review finding #6 — the host-cancellation re-raise must use the
    original CancelledError instance (bare ``raise``), not a fresh one, so
    arq sees the cancel reason it sent and the original traceback survives.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    async def long_running_events(spec, ctx):
        yield ("chunk", {"content": "x", "type": "text"})
        # Block long enough for the cancel to land in this await.
        await asyncio.sleep(5)

    monkeypatch.setattr(run_core, "_iter_agent_events", long_running_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _noop)

    task = asyncio.create_task(run_core.execute_run(_spec()))
    # Give the first chunk a moment to flow through; then cancel with a
    # specific reason that we expect to survive the re-raise.
    await asyncio.sleep(0.05)
    task.cancel("worker SIGTERM, graceful shutdown")

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await task

    # The cancel-reason supplied via task.cancel(msg) survives the cleanup
    # path. A fresh ``raise asyncio.CancelledError()`` would drop the args.
    assert excinfo.value.args == ("worker SIGTERM, graceful shutdown",)


async def test_execute_run_cancellation_cleanup_survives_second_cancel(monkeypatch):
    """Review finding #3 — when a second cancel arrives during the
    interrupted cleanup (SIGKILL grace window), ``asyncio.shield`` must keep
    mark_terminal + append + set_ttl running to completion so the doc isn't
    stranded in ``running`` with no terminal stream frame."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    mark_started = asyncio.Event()
    mark_finished = asyncio.Event()

    async def slow_mark_terminal(run_id, **kwargs):
        mark_started.set()
        # The cleanup is in flight; arrange for the OUTER task to be
        # cancelled while we're awaiting this sleep.
        await asyncio.sleep(0.1)
        mark_finished.set()

    async def fake_events(spec, ctx):
        yield ("chunk", {"content": "partial ", "type": "text"})
        raise asyncio.CancelledError()

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal",
        slow_mark_terminal,
    )

    task = asyncio.create_task(run_core.execute_run(_spec()))
    await asyncio.wait_for(mark_started.wait(), timeout=1.0)

    # Second cancel arrives while mark_terminal is still running. Without
    # shield this would abort the cleanup mid-flight; with shield, the
    # cleanup task continues to completion in the background.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(mark_finished.wait(), timeout=2.0)


async def fake_agent_events_raising(spec, ctx):
    yield ("chunk", {"content": "partial ", "type": "text"})
    raise RuntimeError("boom")


async def test_execute_run_failure_marks_failed_with_error(monkeypatch):
    """When the agent loop raises, execute_run must (a) write an ``error``
    SSE frame, (b) mark the doc ``failed`` with the error message, and
    (c) preserve any partial text already produced."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)

    persisted: list[str] = []
    mark_calls: list[dict[str, Any]] = []

    async def _track_persist(*a, **k):
        persisted.append("called")
        return "should-not-happen"

    async def _track_terminal(run_id, *, status, partial_text="", error=None, **k):
        mark_calls.append(
            {
                "run_id": run_id,
                "status": status,
                "partial_text": partial_text,
                "error": error,
            }
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", fake_agent_events_raising)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _track_persist)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    await run_core.execute_run(_spec())

    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    event_names = [e.event for e in events]
    assert "error" in event_names
    err = next(e for e in events if e.event == "error")
    assert err.data["code"] == "agent.run_failed"
    assert "boom" in err.data["message"]
    assert persisted == []
    assert mark_calls == [
        {
            "run_id": "r1",
            "status": "failed",
            "partial_text": "partial ",
            "error": "boom",
        }
    ]
