# tests/cloud/runs/test_run_heartbeat.py
# Created 2026-09-27 (fix/chat-run-heartbeat). Unit coverage for the pieces behind
# test_long_run_survives.py: the conditional queued -> running claim, the
# heartbeat write and its interval knob, the sweeper's conditional write losing a
# race to the worker cleanly, the worker draining shielded run cleanups before it
# closes the database, and the cut-off marker on the history / wire mappers.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.cloud.chat.runs import run_core, sweeper
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs import worker as chat_worker
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.sessions.service import _message_to_dict

pytestmark = pytest.mark.asyncio


def _spec(run_id: str = "r1") -> RunSpec:
    return RunSpec(
        run_id=run_id,
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def test_mark_running_claims_only_a_queued_run(mongo_db):  # noqa: ARG001
    await run_service.create_run(_spec())

    assert await run_service.mark_running("r1") is True
    doc = await run_service.get_run("r1")
    assert doc.status == "running"
    assert doc.started_at is not None
    assert doc.last_heartbeat_at is not None

    # A second claim (a duplicate delivery) loses.
    assert await run_service.mark_running("r1") is False


async def test_mark_running_does_not_revive_an_interrupted_run(mongo_db):  # noqa: ARG001
    await run_service.create_run(_spec())
    await run_service.mark_terminal("r1", status="interrupted")

    assert await run_service.mark_running("r1") is False
    assert (await run_service.get_run("r1")).status == "interrupted"


async def test_touch_heartbeat_only_stamps_a_running_run(mongo_db):  # noqa: ARG001
    await run_service.create_run(_spec())
    assert await run_service.touch_heartbeat("r1") is False  # still queued

    await run_service.mark_running("r1")
    assert await run_service.touch_heartbeat("r1") is True

    await run_service.mark_completed("r1", assistant_message_id=None, partial_text="")
    assert await run_service.touch_heartbeat("r1") is False
    assert (await run_service.get_run("r1")).status == "completed"


async def test_the_heartbeat_loop_keeps_stamping_until_stopped(mongo_db):  # noqa: ARG001
    await run_service.create_run(_spec())
    await run_service.mark_running("r1")
    old = datetime.now(UTC) - timedelta(minutes=30)
    await run_service.ChatRunDoc.get_pymongo_collection().update_one(
        {"run_id": "r1"}, {"$set": {"last_heartbeat_at": old}}
    )

    task = asyncio.create_task(run_core._heartbeat_loop("r1", 0.01))
    await asyncio.sleep(0.1)
    await run_core._stop_heartbeat(task)

    assert task.done()
    beat = (await run_service.get_run("r1")).last_heartbeat_at
    beat = beat.replace(tzinfo=UTC) if beat.tzinfo is None else beat
    assert beat > old + timedelta(minutes=29)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 30.0), ("5", 5.0), ("0.5", 0.5), ("abc", 30.0), ("0", 30.0), ("-3", 30.0), ("nan", 30.0)],
)
async def test_heartbeat_interval_knob_is_fail_soft(monkeypatch, raw, expected):
    monkeypatch.setenv("POCKETPAW_CLOUD_RUN_HEARTBEAT_SECONDS", raw)
    assert run_core._heartbeat_seconds() == expected


async def test_the_sweeper_loses_a_race_to_the_worker_cleanly(monkeypatch, mongo_db):  # noqa: ARG001
    """The worker completes the run between the sweeper's read and its write.

    The old full-doc ``save()`` overwrote ``completed`` with ``interrupted``; the
    conditional write must miss, count nothing, and add no stream frame.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    await run_service.create_run(_spec())
    await run_service.mark_running("r1")
    await run_service.ChatRunDoc.get_pymongo_collection().update_one(
        {"run_id": "r1"},
        {"$set": {"last_heartbeat_at": datetime.now(UTC) - timedelta(minutes=30)}},
    )
    await transport.append_event("r1", "chunk", {"content": "x"})

    async def _sweep_with_race():
        orig_find = sweeper.ChatRunDoc.find

        class _Racing:
            def __init__(self, q):
                self._q = q

            def limit(self, n):
                self._q = self._q.limit(n)
                return self

            async def to_list(self):
                docs = await self._q.to_list()
                # The worker finishes AFTER the stale read, BEFORE the write.
                await run_service.mark_completed(
                    "r1", assistant_message_id="m-final", partial_text="done"
                )
                return docs

        monkeypatch.setattr(sweeper.ChatRunDoc, "find", lambda *a, **k: _Racing(orig_find(*a, **k)))
        monkeypatch.setattr(sweeper, "_resolve_transport", lambda: transport)
        return await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert await _sweep_with_race() == 0
    doc = await run_service.get_run("r1")
    assert doc.status == "completed"
    assert doc.assistant_message_id == "m-final"
    names = [ev.event async for ev in transport.read_events("r1", after="0", block_ms=10)]
    assert "interrupted" not in names


async def test_shutdown_drains_run_cleanups_before_closing_the_db(monkeypatch):
    """A cancelled run's shielded cleanup must finish before the DB goes away."""
    order: list[str] = []
    release = asyncio.Event()

    async def _cleanup():
        await release.wait()
        order.append("cleanup done")

    async def _close():
        order.append("db closed")

    task = asyncio.ensure_future(_cleanup())
    run_core._track_cleanup(task)
    monkeypatch.setattr(chat_worker, "_bootstrap", AsyncMock())
    monkeypatch.setattr(chat_worker, "close_cloud_db", _close)

    await chat_worker._startup({})
    shutdown = asyncio.create_task(chat_worker._shutdown({}))
    await asyncio.sleep(0.05)
    assert order == [], "the DB was closed while a run cleanup was still writing"
    release.set()
    await shutdown

    assert order == ["cleanup done", "db closed"]
    assert task not in run_core._pending_cleanups


async def test_drain_pending_cleanups_is_bounded_and_never_raises():
    async def _stuck():
        await asyncio.Event().wait()

    async def _boom():
        raise RuntimeError("cleanup blew up")

    stuck = asyncio.ensure_future(_stuck())
    boom = asyncio.ensure_future(_boom())
    run_core._track_cleanup(stuck)
    run_core._track_cleanup(boom)
    try:
        await run_core.drain_pending_cleanups(timeout=0.05)
        assert boom.done()
        assert not stuck.done()
    finally:
        stuck.cancel()
        await asyncio.wait({stuck})


async def test_history_pairs_a_cut_off_message_with_its_note():
    cut = SimpleNamespace(role="assistant", content="half an answer", run_status="interrupted")
    whole = SimpleNamespace(role="assistant", content="a full answer", run_status=None)

    entries = agent_service._message_entries(cut)
    assert [e["role"] for e in entries] == ["assistant", "system"]
    assert "interrupted" in entries[1]["content"]
    assert agent_service._message_entries(whole) == [
        {"role": "assistant", "content": "a full answer"}
    ]


async def test_session_history_wire_marks_only_cut_off_replies():
    base = {
        "id": "x",
        "content": "c",
        "sender": None,
        "sender_type": "agent",
        "createdAt": datetime.now(UTC),
        "attachments": [],
    }
    assert "runStatus" not in _message_to_dict(
        SimpleNamespace(**base, run_status=None), "assistant"
    )
    wire = _message_to_dict(SimpleNamespace(**base, run_status="failed"), "assistant")
    assert wire["runStatus"] == "failed"


async def test_chat_wire_marks_only_cut_off_replies():
    from pocketpaw_ee.cloud.chat.domain import Message as DomainMessage
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict

    fields = {
        "id": "m1",
        "context_type": "group",
        "workspace_id": "w1",
        "group": "g1",
        "sender": None,
        "sender_type": "agent",
        "sender_name": None,
        "agent": "a1",
        "content": "c",
    }
    assert "runStatus" not in message_to_wire_dict(DomainMessage(**fields))
    wire = message_to_wire_dict(DomainMessage(**fields, run_status="cancelled"))
    assert wire["runStatus"] == "cancelled"
