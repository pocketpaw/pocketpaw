# tests/cloud/runs/test_run_core_supervisor_cancel.py
# A supervised turn that is stopped before the backend reaches ``done`` leaves
# its leased warm client mid-reply: the CLI is still writing the rest of that
# answer into the pipe. Reusing the client would hand the NEXT turn this turn's
# tail (a live repro answered "PONG?" with the previous turn's numbers). So a
# cancelled supervised turn demotes the runtime with ``mark_crashed`` exactly like
# a genuine crash, and a turn that completed keeps its warm slot.
# Draining the cancelled steps and closing the backend generator are bounded: a
# step that ignores its cancel, a wedged close, or a cancel that lands during
# either still releases the busy counter and detaches the sinks.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.runs import run_core

from tests.cloud.runs.test_run_core_session_supervisor import (
    _FakeRuntimeService,
    _FakeStore,
    _RecordingSupervisor,
    _scope_ctx,
)

pytestmark = pytest.mark.asyncio


class _SlowPool:
    """Yields one chunk, then hangs as a long reply would."""

    def __init__(self) -> None:
        self.first_chunk_sent = False

    async def get(self, _agent_id):
        return SimpleNamespace(config={"backend": "claude_agent_sdk"}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        async def _gen():
            yield SimpleNamespace(type="message", content="partial", metadata={})
            self.first_chunk_sent = True
            await asyncio.Event().wait()
            yield SimpleNamespace(type="done", content="")  # pragma: no cover

        return _gen()


def _wire(monkeypatch, pool, sup) -> None:
    monkeypatch.setenv("POCKETPAW_SESSION_SUPERVISOR", "true")
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)

    async def _no_knowledge(*a, **k):
        return ""

    monkeypatch.setattr(run_core, "build_knowledge_context", _no_knowledge)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda *a, **k: None)
    monkeypatch.setattr(run_core, "runtime_service", _FakeRuntimeService(prior="native-1"))
    monkeypatch.setattr(run_core, "MongoSessionStore", _FakeStore)
    monkeypatch.setattr(run_core, "get_session_supervisor", lambda: sup)


def _loop(is_cancelled) -> Any:
    return run_core._drive_agent_loop(
        _scope_ctx(),
        user_content="hi",
        attachments_in=None,
        mentions_in=None,
        history=[],
        is_cancelled=is_cancelled,
        emit_stream_start=False,
    )


async def test_user_stop_mid_reply_demotes_the_warm_slot(monkeypatch):
    pool = _SlowPool()
    sup = _RecordingSupervisor()
    _wire(monkeypatch, pool, sup)

    async def _stop_after_first_chunk():
        return pool.first_chunk_sent

    out = [ev async for ev in _loop(_stop_after_first_chunk)]
    assert any(name == "chunk" for name, _ in out)
    assert "mark_crashed" in sup.calls, "a stopped turn left its dirty client warm"
    assert sup.calls[-1] == "mark_run_end"


async def test_consumer_close_mid_reply_demotes_the_warm_slot(monkeypatch):
    pool = _SlowPool()
    sup = _RecordingSupervisor()
    _wire(monkeypatch, pool, sup)

    async def _never():
        return False

    gen = _loop(_never)
    async for name, _payload in gen:
        if name == "chunk":
            break
    await gen.aclose()
    assert "mark_crashed" in sup.calls, "an abandoned turn left its dirty client warm"


class _StuckClosePool:
    """Yields one chunk, then hangs; its ``aclose`` hangs too, as a wedged CLI can."""

    def __init__(self) -> None:
        self.first_chunk_sent = False
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, _agent_id):
        return SimpleNamespace(config={"backend": "claude_agent_sdk"}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        pool = self

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not pool.first_chunk_sent:
                    pool.first_chunk_sent = True
                    return SimpleNamespace(type="message", content="partial", metadata={})
                await asyncio.Event().wait()

            async def aclose(self):
                pool.closing.set()
                await pool.release.wait()

        return _Iter()


def _wire_detach_log(monkeypatch) -> list[str]:
    detached: list[str] = []
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda *a, **k: detached.append("sink"))
    monkeypatch.setattr(
        run_core, "detach_agent_identity", lambda *a, **k: detached.append("identity")
    )
    return detached


async def test_hanging_backend_close_does_not_block_run_end_bookkeeping(monkeypatch):
    pool = _StuckClosePool()
    sup = _RecordingSupervisor()
    _wire(monkeypatch, pool, sup)
    detached = _wire_detach_log(monkeypatch)
    monkeypatch.setattr(run_core, "_ACLOSE_TIMEOUT_SECONDS", 0.05)

    async def _stop_after_first_chunk():
        return pool.first_chunk_sent

    async def _consume():
        return [ev async for ev in _loop(_stop_after_first_chunk)]

    try:
        await asyncio.wait_for(_consume(), 2)
    finally:
        pool.release.set()
    assert pool.closing.is_set(), "the backend generator was never closed"
    assert detached == ["sink", "identity"]
    assert sup.calls[-1] == "mark_run_end", "a wedged close leaked the busy counter"


async def test_cancel_during_backend_close_still_runs_run_end_bookkeeping(monkeypatch):
    pool = _StuckClosePool()
    sup = _RecordingSupervisor()
    _wire(monkeypatch, pool, sup)
    detached = _wire_detach_log(monkeypatch)

    async def _stop_after_first_chunk():
        return pool.first_chunk_sent

    async def _consume():
        return [ev async for ev in _loop(_stop_after_first_chunk)]

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(pool.closing.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        pool.release.set()
    assert detached == ["sink", "identity"]
    assert sup.calls[-1] == "mark_run_end", "a cancel during close leaked the busy counter"


class _StuckStepPool:
    """Yields one chunk; the next step ignores its cancel and hangs, as a wedged SDK read can."""

    def __init__(self) -> None:
        self.first_chunk_sent = False
        self.stepping = asyncio.Event()
        self.step_cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, _agent_id):
        return SimpleNamespace(config={"backend": "claude_agent_sdk"}, agent_name="A")

    def run(self, agent_id, content, session_key, **kwargs):
        pool = self

        class _Iter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not pool.first_chunk_sent:
                    pool.first_chunk_sent = True
                    return SimpleNamespace(type="message", content="partial", metadata={})
                pool.stepping.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pool.step_cancelled.set()
                    await pool.release.wait()
                raise StopAsyncIteration

            async def aclose(self):
                return None

        return _Iter()


async def test_hanging_pending_step_does_not_block_run_end_bookkeeping(monkeypatch):
    pool = _StuckStepPool()
    sup = _RecordingSupervisor()
    _wire(monkeypatch, pool, sup)
    detached = _wire_detach_log(monkeypatch)
    monkeypatch.setattr(run_core, "_ACLOSE_TIMEOUT_SECONDS", 0.05)

    async def _stop_after_first_chunk():
        await asyncio.sleep(0)  # let the next step start, so the stop finds it pending
        return pool.stepping.is_set()

    async def _consume():
        return [ev async for ev in _loop(_stop_after_first_chunk)]

    try:
        await asyncio.wait_for(_consume(), 2)
    finally:
        pool.release.set()
    assert pool.step_cancelled.is_set(), "the pending step was never cancelled"
    assert detached == ["sink", "identity"]
    assert sup.calls[-1] == "mark_run_end", "a wedged step leaked the busy counter"


async def test_cancel_while_draining_pending_step_still_runs_run_end_bookkeeping(monkeypatch):
    pool = _StuckStepPool()
    sup = _RecordingSupervisor()
    _wire(monkeypatch, pool, sup)
    detached = _wire_detach_log(monkeypatch)

    async def _stop_after_first_chunk():
        await asyncio.sleep(0)  # let the next step start, so the stop finds it pending
        return pool.stepping.is_set()

    async def _consume():
        return [ev async for ev in _loop(_stop_after_first_chunk)]

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(pool.step_cancelled.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        pool.release.set()
    assert detached == ["sink", "identity"]
    assert sup.calls[-1] == "mark_run_end", "a cancel while draining leaked the busy counter"
