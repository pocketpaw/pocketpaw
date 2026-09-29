# tests/cloud/runs/test_run_core_supervisor_cancel.py
# A supervised turn that is stopped before the backend reaches ``done`` leaves
# its leased warm client mid-reply: the CLI is still writing the rest of that
# answer into the pipe. Reusing the client would hand the NEXT turn this turn's
# tail (a live repro answered "PONG?" with the previous turn's numbers). So a
# cancelled supervised turn demotes the runtime with ``mark_crashed`` exactly like
# a genuine crash, and a turn that completed keeps its warm slot.

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
