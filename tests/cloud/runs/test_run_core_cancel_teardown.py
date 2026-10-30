"""A user stop closes the agent event generator before the run is marked terminal.

``execute_run`` leaves its ``async for`` over ``_iter_agent_events`` with a
``break`` when the cancel flag is set. Without an explicit ``aclose`` the
generator's ``finally`` (the backend close, sink and supervisor bookkeeping in
``_drive_agent_loop``) waits for garbage collection, so a superseding run could
start while the cancelled one's backend is still alive. The terminal status is
what a superseding message waits on, so the teardown has to land before it.
"""

from __future__ import annotations

import asyncio

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


async def _fake_resolve_scope_context(**_):
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


async def test_cancel_closes_the_agent_generator_before_the_terminal_write(monkeypatch):
    transport = RedisStreamTransport(fakeredis.aioredis.FakeRedis(decode_responses=True))
    order: list[str] = []

    async def agent_events(spec, ctx):  # noqa: ARG001
        try:
            yield ("chunk", {"content": "partial ", "type": "text"})
            yield ("chunk", {"content": "never read", "type": "text"})
        finally:
            # A real backend close takes a moment (``_drive_agent_loop`` awaits the
            # backend's own ``aclose``). A close left to the GC finalizer is only
            # scheduled, so it finishes after the terminal write.
            await asyncio.sleep(0.05)
            order.append("generator_closed")

    async def track_terminal(run_id, *, status, **k):  # noqa: ARG001
        order.append(f"mark_terminal:{status}")

    monkeypatch.setattr(run_core, "_iter_agent_events", agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_persist_partial_reply", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", _fake_resolve_scope_context)
    monkeypatch.setattr(run_core.run_service, "mark_terminal", track_terminal)

    await transport.request_cancel("r1")
    await run_core.execute_run(_spec())

    assert order == ["generator_closed", "mark_terminal:cancelled"]
