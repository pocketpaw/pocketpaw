# tests/cloud/runs/test_long_run_survives.py
# Created 2026-09-27 (fix/chat-run-heartbeat). Reproduces the reported bug: "if the
# agent takes a lot of time the worker stops completely — nothing that was emitted
# is saved, only the user message after refresh."
#
# Three causes, one test group each:
#
# 1. The web process's stale-run sweeper judges a run by ``createdAt``. Any run
#    older than 10 minutes is flipped to ``interrupted`` and gets an
#    ``interrupted`` frame on its live stream, even though the worker is still
#    driving it. The browser's stream ends and ``active_run`` goes null, so a
#    refresh does not re-attach. Fix: the worker heartbeats the run doc and the
#    sweeper judges RUNNING runs by that heartbeat, with conditional writes.
#
# 2. A run that ends any way other than ``completed`` writes no assistant
#    ``Message``. Its text lands on ``ChatRunDoc.partial_text``, which the chat
#    history the UI reads never looks at. Fix: persist the partial as a real
#    Message flagged as cut off, and do not count it a second time in the
#    agent's own history (the stranded-reply path).
#
# 3. arq's ``job_timeout`` cancels the task. The CancelledError cleanup marks the
#    doc interrupted but, like (2), never writes the Message.
#
# EXPECTED STATE ON THE UNFIXED TREE: every test in the "bug" sections fails; the
# characterization at the bottom passes.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    load_history_for_scope,
    session_key_for,
)
from pocketpaw_ee.cloud.chat.runs import run_core, sweeper
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.message import Message

pytestmark = pytest.mark.asyncio

_PARTIAL = "I checked the first four repositories and"


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
        content="audit every repo",
        history=[],
        intent=None,
    )


def _ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )


async def _noop(*a, **k):
    return None


class _FakePool:
    async def observe(self, *a, **k):
        return None


def _wire(monkeypatch, transport, ctx, agent_events, *, real_mark_running: bool = False):
    """Point execute_run at fakes for everything except Mongo and the run service."""

    async def fake_resolve_scope_context(**_):
        return ctx

    monkeypatch.setattr(run_core, "_iter_agent_events", agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(sweeper, "get_stream_transport", lambda: transport)
    if not real_mark_running:
        monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)


async def _backdate(run_id: str, *, minutes: int) -> None:
    doc = await run_service.get_run(run_id)
    doc.createdAt = datetime.now(UTC) - timedelta(minutes=minutes)
    await doc.save()


async def _stream_event_names(transport: RedisStreamTransport, run_id: str) -> list[str]:
    return [ev.event async for ev in transport.read_events(run_id, after="0", block_ms=10)]


async def _assistant_messages(ctx: ScopeContext) -> list[Message]:
    return await Message.find(
        {"session_key": session_key_for(ctx), "role": "assistant", "workspace_id": "w1"}
    ).to_list()


# ---------------------------------------------------------------------------
# 1. The sweeper must not interrupt a run the worker is still driving
# ---------------------------------------------------------------------------


async def test_sweeper_leaves_a_long_but_live_run_running(monkeypatch, mongo_db):  # noqa: ARG001
    """A run started 30 minutes ago that the worker is still driving is not stale.

    The sweep fires while the agent is mid-answer, exactly like the web
    process's 5-minute tick does in production. The run must still finish as
    ``completed`` with its Message written, and its stream must never carry the
    sweeper's ``interrupted`` frame.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())
    await _backdate("r1", minutes=30)

    async def slow_agent(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": _PARTIAL, "type": "text"})
        # The web process's periodic sweep lands mid-run.
        await sweeper.sweep_stale_runs()
        yield ("chunk", {"content": " all of them pass.", "type": "text"})

    _wire(monkeypatch, transport, ctx, slow_agent, real_mark_running=True)

    await run_core.execute_run(_spec())

    doc = await run_service.get_run("r1")
    assert doc.status == "completed", (
        f"the sweeper judged a live run by createdAt and flipped it to {doc.status!r}"
    )
    names = await _stream_event_names(transport, "r1")
    assert "interrupted" not in names, (
        "the sweeper wrote a terminal 'interrupted' frame onto a live run's stream, "
        "so the browser stopped listening mid-answer"
    )
    assert [m.content for m in await _assistant_messages(ctx)] == [f"{_PARTIAL} all of them pass."]


async def test_sweeper_judges_running_runs_by_heartbeat(mongo_db):  # noqa: ARG001
    """Old ``createdAt`` + fresh heartbeat is alive; an old heartbeat is stale."""
    now = datetime.now(UTC)
    for run_id, beat_minutes_ago in (("alive", 1), ("dead", 20)):
        await ChatRunDoc(
            run_id=run_id,
            workspace="w1",
            context_type="session",
            scope_id="s1",
            session_key="k1",
            user_id="u1",
            agent_id="a1",
            client_message_id=f"c-{run_id}",
            user_message_id="um1",
            status="running",
            createdAt=now - timedelta(minutes=40),
            started_at=now - timedelta(minutes=40),
        ).insert()
        await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id).update(
            {"$set": {"last_heartbeat_at": now - timedelta(minutes=beat_minutes_ago)}}
        )

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 1
    assert (await run_service.get_run("alive")).status == "running"
    assert (await run_service.get_run("dead")).status == "interrupted"


async def test_worker_does_not_run_a_job_the_sweeper_already_interrupted(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """A queued run swept to ``interrupted`` (its client already got the terminal
    frame) must not be picked up and driven anyway — nobody is listening, and
    ``mark_running`` used to flip it straight back to ``running``."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())
    await _backdate("r1", minutes=30)

    agent_ran = False

    async def agent(spec, ctx):  # noqa: ARG001
        nonlocal agent_ran
        agent_ran = True
        yield ("chunk", {"content": "late answer", "type": "text"})

    _wire(monkeypatch, transport, ctx, agent, real_mark_running=True)
    await sweeper.sweep_stale_runs(older_than_minutes=10)

    await run_core.execute_run(_spec())

    assert agent_ran is False
    assert (await run_service.get_run("r1")).status == "interrupted"
    assert await _assistant_messages(ctx) == []


# ---------------------------------------------------------------------------
# 2 + 3. A run that does not complete still leaves its reply in the chat
# ---------------------------------------------------------------------------


async def test_a_timed_out_run_saves_its_partial_reply_as_a_message(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """arq's ``job_timeout`` cancels the task mid-run. The text already streamed
    must be a Message the chat history returns, not only ``partial_text``."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def hanging_agent(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": _PARTIAL, "type": "text"})
        await asyncio.Event().wait()  # a tool call that never returns
        yield ("chunk", {"content": "unreachable", "type": "text"})

    _wire(monkeypatch, transport, ctx, hanging_agent)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(run_core.execute_run(_spec()), timeout=0.5)
    # The cleanup is shielded and may still be finishing in the background.
    await run_core.drain_pending_cleanups(timeout=5)

    doc = await run_service.get_run("r1")
    assert doc.status == "interrupted"
    messages = await _assistant_messages(ctx)
    assert [m.content for m in messages] == [_PARTIAL], (
        "the timed-out run's text reached only ChatRunDoc.partial_text, so the "
        "chat shows nothing but the user message after a refresh"
    )
    assert doc.assistant_message_id == str(messages[0].id)


@pytest.mark.parametrize(
    ("events", "cancel", "status"),
    [
        (
            [
                ("chunk", {"content": _PARTIAL, "type": "text"}),
                ("error", {"code": "agent.run_failed", "message": "provider exploded"}),
            ],
            False,
            "failed",
        ),
        ([("chunk", {"content": _PARTIAL, "type": "text"})], True, "cancelled"),
    ],
    ids=["failed", "cancelled"],
)
async def test_a_run_that_does_not_complete_saves_its_partial_as_a_message(
    monkeypatch,
    mongo_db,  # noqa: ARG001
    events,
    cancel,
    status,
):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def agent(spec, ctx):  # noqa: ARG001
        for name, data in events:
            yield (name, data)

    _wire(monkeypatch, transport, ctx, agent)
    if cancel:
        await transport.request_cancel("r1")

    await run_core.execute_run(_spec())

    doc = await run_service.get_run("r1")
    assert doc.status == status
    messages = await _assistant_messages(ctx)
    assert [m.content for m in messages] == [_PARTIAL]
    assert doc.assistant_message_id == str(messages[0].id)


async def test_a_persisted_partial_is_not_counted_twice_in_agent_history(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """Once the partial is a Message, the stranded-reply path must not add it
    again. The agent still has to be told the reply was cut off."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def agent(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": _PARTIAL, "type": "text"})
        yield ("error", {"code": "agent.run_failed", "message": "provider exploded"})

    _wire(monkeypatch, transport, ctx, agent)
    await run_core.execute_run(_spec())

    history = await load_history_for_scope(ctx)
    assistant = [m["content"] for m in history if m["role"] == "assistant"]
    assert assistant == [_PARTIAL]
    system = [m["content"] for m in history if m["role"] == "system"]
    assert len(system) == 1 and "failed" in system[0], (
        "the agent lost the note that its previous reply was cut off"
    )


# ---------------------------------------------------------------------------
# Characterization — passes today
# ---------------------------------------------------------------------------


async def test_a_run_that_emitted_no_text_writes_no_message(monkeypatch, mongo_db):  # noqa: ARG001
    """Nothing to show, nothing written: a failed run with no text stays out of
    the chat entirely (the client's error row covers it)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def agent(spec, ctx):  # noqa: ARG001
        yield ("error", {"code": "agent.run_failed", "message": "provider exploded"})

    _wire(monkeypatch, transport, ctx, agent)
    await run_core.execute_run(_spec())

    assert (await run_service.get_run("r1")).status == "failed"
    assert await _assistant_messages(ctx) == []


# ---------------------------------------------------------------------------
# Review follow-ups (PR #2266)
# ---------------------------------------------------------------------------


def _live_heartbeats() -> list[asyncio.Task]:
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done() and getattr(t.get_coro(), "__name__", "") == "_heartbeat_loop"
    ]


async def test_a_failed_typing_broadcast_does_not_leak_the_heartbeat(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """The typing broadcast rides Redis in worker mode. When it fails, the run must
    still finish, and no heartbeat may keep beating for a run nobody drives —
    that is a phantom ``active_run`` the sweeper can never clear."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def agent(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "Done.", "type": "text"})

    _wire(monkeypatch, transport, ctx, agent, real_mark_running=True)

    async def flaky_typing(_ctx, *, active):
        if active:
            raise ConnectionError("redis blip")

    monkeypatch.setattr(run_core, "_broadcast_agent_typing", flaky_typing)

    await run_core.execute_run(_spec())

    assert _live_heartbeats() == []
    assert (await run_service.get_run("r1")).status == "completed"


async def test_the_heartbeat_loop_stops_once_the_run_is_not_running(monkeypatch):
    async def not_running(_run_id):
        return False

    monkeypatch.setattr(run_service, "touch_heartbeat", not_running)

    await asyncio.wait_for(run_core._heartbeat_loop("r1", 0.01), timeout=1)


async def test_a_superseded_partial_sorts_before_the_next_user_message(
    monkeypatch,
    mongo_db,  # noqa: ARG001
):
    """Sending a new message cancels the running turn, and the new user message
    is written before the worker notices. The cut-off reply belongs to the turn
    it answered, so it must sort before that newer message."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    ctx = _ctx()
    await run_service.create_run(_spec())

    async def agent(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": _PARTIAL, "type": "text"})
        await asyncio.sleep(0.01)
        await Message(
            context_type="session",
            session_key=session_key_for(ctx),
            role="user",
            content="actually, just the api repo",
            workspace_id="w1",
        ).insert()
        await transport.request_cancel("r1")
        yield ("chunk", {"content": " more", "type": "text"})

    _wire(monkeypatch, transport, ctx, agent, real_mark_running=True)
    await run_core.execute_run(_spec())

    rows = (
        await Message.find({"session_key": session_key_for(ctx), "workspace_id": "w1"})
        .sort("createdAt")
        .to_list()
    )
    assert [(m.role, m.run_status) for m in rows] == [
        ("assistant", "cancelled"),
        ("user", None),
    ]


async def test_the_boot_sweep_leaves_runs_alive_on_another_replica():
    """The boot sweep's cutoff must clear a few heartbeat intervals, or a booting
    replica interrupts runs another replica is still beating."""
    from pocketpaw_ee.cloud.chat.runs import worker as chat_worker

    assert chat_worker._boot_sweep_older_than_seconds() >= 2 * run_core._heartbeat_seconds()
