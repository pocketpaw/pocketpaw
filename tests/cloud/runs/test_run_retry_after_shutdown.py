# tests/cloud/runs/test_run_retry_after_shutdown.py
#
# A chat run the WORKER killed (a deploy or restart outlasting the drain window) is
# retried once, automatically, instead of ending ``interrupted`` for the user to resend.
#
# The retry has two halves. The cancelled attempt puts its run back to ``queued``
# (``attempt`` + 1, no partial Message, a non-terminal ``retrying`` frame instead of the
# terminal ``interrupted`` one), and arq re-runs the job on the next worker because
# ``execute_run_job`` allows two tries. The second attempt skips the run-start gates
# that SPEND (guest turn, daily turn ceiling, early-access reservation): the run already
# paid them once, and a deploy must not charge a user twice for one message.
#
# What must NOT retry: a run whose own job_timeout fired (no shutdown in progress), a
# run that already called tools (re-running could repeat a side effect), and a run on
# its second attempt. Those keep today's behaviour: partial saved, ``interrupted``.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud._core import shutdown
from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind, session_key_for
from pocketpaw_ee.cloud.chat.runs import run_core, sweeper, worker
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import MAX_RUN_ATTEMPTS, RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.models.message import Message

pytestmark = pytest.mark.asyncio

_PARTIAL = "I read the first two files and"


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
        content="summarise the repo",
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


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    shutdown.reset()
    yield
    shutdown.reset()


@pytest.fixture
def transport() -> RedisStreamTransport:
    return RedisStreamTransport(fakeredis.aioredis.FakeRedis(decode_responses=True))


def _wire(monkeypatch, transport, agent_events) -> ScopeContext:
    ctx = _ctx()

    async def fake_resolve_scope_context(**_):
        return ctx

    monkeypatch.setattr(run_core, "_iter_agent_events", agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(sweeper, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "_broadcast_message_new", _noop)
    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    return ctx


def _hanging_agent(*, tool: bool = False):
    async def agent(spec, ctx):  # noqa: ARG001
        if tool:
            yield ("tool_start", {"tool": "send_email", "input": {"to": "a@b.c"}})
            yield ("tool_result", {"tool": "send_email", "output": "sent"})
        yield ("chunk", {"content": _PARTIAL, "type": "text"})
        await asyncio.Event().wait()  # still generating when the worker goes away
        yield ("chunk", {"content": "unreachable", "type": "text"})

    return agent


async def _kill_mid_run(*, at_shutdown: bool) -> None:
    """Start ``execute_run`` and cancel it the way the worker's close() does."""
    task = asyncio.create_task(run_core.execute_run(_spec()))
    await asyncio.sleep(0.2)
    if at_shutdown:
        shutdown.mark_stopping()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await run_core.drain_pending_cleanups(timeout=5)


async def _event_names(transport: RedisStreamTransport) -> list[str]:
    return [ev.event async for ev in transport.read_events("r1", after="0", block_ms=10)]


async def _assistant_messages(ctx: ScopeContext) -> list[Message]:
    return await Message.find(
        {"session_key": session_key_for(ctx), "role": "assistant", "workspace_id": "w1"}
    ).to_list()


# ---------------------------------------------------------------------------
# The bug: a run the worker killed on shutdown is never retried
# ---------------------------------------------------------------------------


async def test_a_run_killed_by_worker_shutdown_is_requeued(monkeypatch, mongo_db, transport):  # noqa: ARG001
    ctx = _wire(monkeypatch, transport, _hanging_agent())
    await run_service.create_run(_spec())

    await _kill_mid_run(at_shutdown=True)

    doc = await run_service.get_run("r1")
    assert doc.status == "queued", "a system-killed run ended terminal instead of retrying"
    assert doc.attempt == 2
    assert doc.requeued_at is not None
    # The retry writes the whole reply; a partial Message now would be a duplicate.
    assert await _assistant_messages(ctx) == []
    assert doc.assistant_message_id is None
    names = await _event_names(transport)
    assert "retrying" in names
    assert "interrupted" not in names, "a terminal frame would make the client give up"


async def test_the_retried_attempt_completes_with_one_reply(monkeypatch, mongo_db, transport):  # noqa: ARG001
    """End to end: attempt 1 killed at shutdown, attempt 2 driven by the next worker."""
    attempts = 0

    async def agent(spec, ctx):  # noqa: ARG001
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield ("chunk", {"content": _PARTIAL, "type": "text"})
            await asyncio.Event().wait()
        yield ("chunk", {"content": "The full answer.", "type": "text"})

    ctx = _wire(monkeypatch, transport, agent)
    await run_service.create_run(_spec())
    await _kill_mid_run(at_shutdown=True)
    shutdown.reset()  # the next worker process starts clean

    await run_core.execute_run(_spec(), retry=True)

    doc = await run_service.get_run("r1")
    assert doc.status == "completed"
    messages = await _assistant_messages(ctx)
    assert [m.content for m in messages] == ["The full answer."]


async def test_execute_run_job_retries_a_requeued_run(monkeypatch, mongo_db):  # noqa: ARG001
    """arq's second try reaches ``execute_run`` flagged as a retry."""
    await run_service.create_run(_spec())
    seen: list[dict] = []

    async def fake_execute_run(spec, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(worker, "execute_run", fake_execute_run)
    monkeypatch.setattr("pocketpaw_ee.cloud.metering.service.bill_run_now", _noop)

    await worker.execute_run_job({"job_try": 2}, _spec().model_dump())

    assert seen == [{"retry": True}]


async def test_execute_run_job_drops_a_retry_whose_run_already_ended(monkeypatch, mongo_db):  # noqa: ARG001
    """arq re-runs every job a shutdown cancelled; only a requeued run may run again."""
    await run_service.create_run(_spec())
    await run_service.mark_terminal("r1", status="interrupted")
    seen: list[str] = []

    async def fake_execute_run(spec, **kwargs):  # noqa: ARG001
        seen.append(spec.run_id)

    monkeypatch.setattr(worker, "execute_run", fake_execute_run)

    await worker.execute_run_job({"job_try": 2}, _spec().model_dump())

    assert seen == []


async def test_the_chat_job_allows_arq_one_retry():
    """arq only re-runs a job cancelled at shutdown while ``job_try <= max_tries``."""
    fn = next(
        f
        for f in worker.WorkerSettings.functions
        if getattr(f, "name", getattr(f, "__name__", "")) == "execute_run_job"
    )
    assert fn.max_tries == MAX_RUN_ATTEMPTS == 2


async def test_a_retry_does_not_spend_the_run_start_gates_again(monkeypatch, mongo_db, transport):  # noqa: ARG001
    """The guest and daily-turn counters were charged by attempt 1."""

    async def refuse(*a, **k):
        raise AssertionError("a retried run spent a run-start gate a second time")

    async def agent(spec, ctx):  # noqa: ARG001
        yield ("chunk", {"content": "done", "type": "text"})

    _wire(monkeypatch, transport, agent)
    monkeypatch.setattr(run_core, "_reject_if_guest_over_limit", refuse)
    monkeypatch.setattr(run_core, "_reject_if_over_daily_turns", refuse)
    await run_service.create_run(_spec())

    await run_core.execute_run(_spec(), retry=True)

    assert (await run_service.get_run("r1")).status == "completed"


# ---------------------------------------------------------------------------
# What must keep ending ``interrupted``
# ---------------------------------------------------------------------------


async def test_a_job_timeout_is_not_retried(monkeypatch, mongo_db, transport):  # noqa: ARG001
    """No shutdown in progress means the run's own timeout fired: retrying would loop."""
    ctx = _wire(monkeypatch, transport, _hanging_agent())
    await run_service.create_run(_spec())

    await _kill_mid_run(at_shutdown=False)

    doc = await run_service.get_run("r1")
    assert doc.status == "interrupted"
    assert [m.content for m in await _assistant_messages(ctx)] == [_PARTIAL]


async def test_a_run_that_called_tools_is_not_retried(monkeypatch, mongo_db, transport):  # noqa: ARG001
    """Re-running from the top would repeat the tool's side effect (an email, a deploy)."""
    ctx = _wire(monkeypatch, transport, _hanging_agent(tool=True))
    await run_service.create_run(_spec())

    await _kill_mid_run(at_shutdown=True)

    doc = await run_service.get_run("r1")
    assert doc.status == "interrupted"
    assert doc.attempt == 1
    assert [m.content for m in await _assistant_messages(ctx)] == [_PARTIAL]
    assert "interrupted" in await _event_names(transport)


async def test_a_second_attempt_is_not_retried_again(monkeypatch, mongo_db, transport):  # noqa: ARG001
    _wire(monkeypatch, transport, _hanging_agent())
    await run_service.create_run(_spec())
    await _kill_mid_run(at_shutdown=True)
    assert (await run_service.get_run("r1")).attempt == 2

    await _kill_mid_run(at_shutdown=True)

    doc = await run_service.get_run("r1")
    assert doc.status == "interrupted"
    assert doc.attempt == 2


# ---------------------------------------------------------------------------
# The web sweeper must give a requeued run its full queued window
# ---------------------------------------------------------------------------


async def test_the_sweeper_times_a_requeued_run_from_its_requeue(mongo_db, transport, monkeypatch):  # noqa: ARG001
    """``createdAt`` is when the user sent it; a retry re-enters the queue later."""
    monkeypatch.setattr(sweeper, "get_stream_transport", lambda: transport)
    doc = await run_service.create_run(_spec())
    doc.createdAt = datetime.now(UTC) - timedelta(minutes=30)
    doc.status = "queued"
    doc.attempt = 2
    doc.requeued_at = datetime.now(UTC)
    await doc.save()

    swept = await sweeper.sweep_stale_runs()

    assert swept == 0
    assert (await run_service.get_run("r1")).status == "queued"


async def test_the_sweeper_still_times_out_a_stale_requeue(mongo_db, transport, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(sweeper, "get_stream_transport", lambda: transport)
    doc = await run_service.create_run(_spec())
    doc.status = "queued"
    doc.requeued_at = datetime.now(UTC) - timedelta(minutes=30)
    await doc.save()

    assert await sweeper.sweep_stale_runs() == 1
