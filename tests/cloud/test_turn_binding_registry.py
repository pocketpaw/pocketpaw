# tests/cloud/test_turn_binding_registry.py
# Pins that in-process MCP tools on a WARM Claude SDK client see the CURRENT
# turn's identity, Paw Bar context, timeline, SSE sink and artifact collector —
# not the ones bound in the task that connected the client.
#
# The SDK starts the in-process MCP handler tasks at ``connect()``, and a task
# copies the context as it stood then. Every later turn on that client runs its
# tools under the connecting task's ContextVars (often the prewarm task's). So a
# pocket member's turn could run tools as whoever connected the client, and from
# turn 2 ``deliver_artifact`` appended to turn 1's collector, which nobody drains.
#
# The fix is a per-session turn slot: the connecting task carries a slot object,
# and each run fills it with THIS turn's binding before it queries the client.
# ``copy_context()`` below stands in for the SDK capturing context at connect.

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat import agent_service as svc
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
from pocketpaw_ee.cloud.chat.runs import run_core

pytestmark = pytest.mark.asyncio


def _ctx(user_id: str, *, pocket_id: str | None = None, kind=ScopeKind.POCKET) -> ScopeContext:
    return ScopeContext(
        kind=kind,
        scope_id="p1",
        workspace_id="w1",
        user_id=user_id,
        members=["u-a", "u-b"],
        target_agent_id="a1",
        pocket_id=pocket_id,
    )


async def test_resolvers_read_the_current_turn_not_the_connect_context():
    key = "cloud:pocket:p1:a1:u:u-a"

    # "Connect": the prewarm task binds the slot and turn-0 identity, and the
    # SDK's handler tasks capture that context.
    slot_token = svc.enter_turn_slot(key)
    ident = svc.attach_agent_identity(workspace_id="w1", user_id="u-a", pocket_id="p-old")
    with svc.collect_delivered_artifacts() as stale:
        connect_ctx = contextvars.copy_context()
    svc.detach_agent_identity(ident)
    svc.exit_turn_slot(slot_token)

    fresh: list[dict[str, Any]] = []
    queue: asyncio.Queue = asyncio.Queue()
    handle = svc.bind_turn(
        key,
        svc.TurnBinding(
            workspace_id="w1",
            user_id="u-a",
            session_mongo_id=None,
            pocket_id="p-new",
            pawbar_run={"widget_id": "w"},
            timeline={"id": "t"},
            sse_sink=queue,
            artifacts=fresh,
        ),
    )
    try:
        assert connect_ctx.run(svc.current_pocket_id) == "p-new"
        assert connect_ctx.run(svc.current_pawbar_run) == {"widget_id": "w"}
        assert connect_ctx.run(svc.current_timeline) == {"id": "t"}
        connect_ctx.run(svc.record_delivered_artifact, {"file_id": "f2"})
        connect_ctx.run(svc.push_sse_event, "pocket_mutation", {"x": 1})
    finally:
        svc.unbind_turn(handle)

    assert fresh == [{"file_id": "f2"}], "turn 2's artifact landed in a stale collector"
    assert stale == []
    assert queue.get_nowait() == ("pocket_mutation", {"x": 1})
    # Unbound: the connect-time values are all that is left.
    assert connect_ctx.run(svc.current_pocket_id) == "p-old"


async def test_a_concurrent_turn_does_not_steal_the_active_binding():
    key = "cloud:session:s1:a1:u:u-a"
    slot_token = svc.enter_turn_slot(key)
    warm_ctx = contextvars.copy_context()
    svc.exit_turn_slot(slot_token)

    first = svc.bind_turn(
        key, svc.TurnBinding(workspace_id="w1", user_id="u-a", pocket_id="p-first")
    )
    # A second turn on the same session while the first still streams: its
    # client is a separate (stateless) one, so it must get a private slot.
    second = svc.bind_turn(
        key, svc.TurnBinding(workspace_id="w1", user_id="u-a", pocket_id="p-second")
    )
    try:
        assert warm_ctx.run(svc.current_pocket_id) == "p-first"
        assert svc.current_pocket_id() == "p-second", "the second run's own context"
    finally:
        svc.unbind_turn(second)
        svc.unbind_turn(first)


async def test_warm_key_is_per_member_in_a_shared_scope():
    a = svc.warm_session_key_for(_ctx("u-a"))
    b = svc.warm_session_key_for(_ctx("u-b"))
    assert a != b, "two members of one pocket must never share a warm CLI process"
    assert svc.session_key_for(_ctx("u-a")) == svc.session_key_for(_ctx("u-b")), (
        "the Mongo history key stays shared"
    )


class _CapturingPool:
    def __init__(self) -> None:
        self.connect_ctx: contextvars.Context | None = None
        self.prewarm_key: str | None = None
        self.run_key: str | None = None
        self.seen: dict[str, Any] = {}

    async def get(self, _agent_id):
        return type("Inst", (), {"config": {"backend": "claude_agent_sdk"}})()

    async def prewarm(self, _agent_id, session_key, **_kw):
        self.prewarm_key = session_key
        self.connect_ctx = contextvars.copy_context()

    def run(self, _agent_id, _prompt, session_key, **_kw):
        self.run_key = session_key
        ctx = self.connect_ctx
        assert ctx is not None
        self.seen["user"] = ctx.run(svc.current_user_id)
        self.seen["pocket"] = ctx.run(svc.current_pocket_id)
        ctx.run(svc.record_delivered_artifact, {"file_id": "turn-2"})

        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


async def test_run_core_binds_the_turn_for_a_client_connected_by_prewarm(monkeypatch):
    pool = _CapturingPool()
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: pool)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda *a, **k: "INSTR")

    async def _fake_knowledge(_ctx, **kwargs):
        return "KB"

    monkeypatch.setattr(run_core, "build_knowledge_context", _fake_knowledge)

    with svc.collect_delivered_artifacts() as turn1:
        await run_core._prewarm_session(_ctx("u-a", pocket_id="p-prewarm"))

    async def _is_cancelled():
        return False

    with svc.collect_delivered_artifacts() as turn2:
        async for _ in run_core._drive_agent_loop(
            _ctx("u-a", pocket_id="p-now"),
            user_content="hi",
            attachments_in=None,
            mentions_in=None,
            history=[],
            is_cancelled=_is_cancelled,
            emit_stream_start=False,
        ):
            pass

    assert pool.prewarm_key == pool.run_key, "prewarm must warm the key the turn uses"
    assert pool.run_key == svc.warm_session_key_for(_ctx("u-a"))
    assert pool.seen["pocket"] == "p-now"
    assert pool.seen["user"] == "u-a"
    assert turn2 == [{"file_id": "turn-2"}] and turn1 == []
