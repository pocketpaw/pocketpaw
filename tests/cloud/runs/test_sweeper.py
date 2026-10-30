"""Stale-run sweeper marks queued/running ChatRunDocs as interrupted when
they've outlived the threshold — the backend process died, the executor task
is gone, but Mongo still says ``running``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.runs import sweeper, transport
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_transport(monkeypatch):
    """Swap the stream transport for an in-memory fakeredis-backed one so the
    sweeper's append step is testable without a live Redis."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = RedisStreamTransport(redis)
    monkeypatch.setattr(transport, "_transport", t)
    monkeypatch.setattr(sweeper, "get_stream_transport", lambda: t)
    yield t
    transport._reset_for_tests()


def _make_run(*, status: str, created_minutes_ago: int) -> ChatRunDoc:
    created = datetime.now(UTC) - timedelta(minutes=created_minutes_ago)
    return ChatRunDoc(
        run_id=f"r-{status}-{created_minutes_ago}",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id=f"c-{status}-{created_minutes_ago}",
        user_message_id="um1",
        status=status,  # type: ignore[arg-type]
        createdAt=created,
    )


async def test_sweep_marks_stale_running_as_interrupted(mongo_db):  # noqa: ARG001
    stale = _make_run(status="running", created_minutes_ago=30)
    fresh = _make_run(status="running", created_minutes_ago=2)
    queued_stale = _make_run(status="queued", created_minutes_ago=30)
    completed = _make_run(status="completed", created_minutes_ago=30)
    await stale.insert()
    await fresh.insert()
    await queued_stale.insert()
    await completed.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 2  # stale + queued_stale
    refreshed = await ChatRunDoc.find_one(ChatRunDoc.run_id == stale.run_id)
    assert refreshed is not None and refreshed.status == "interrupted"
    refreshed_fresh = await ChatRunDoc.find_one(ChatRunDoc.run_id == fresh.run_id)
    assert refreshed_fresh is not None and refreshed_fresh.status == "running"
    refreshed_completed = await ChatRunDoc.find_one(ChatRunDoc.run_id == completed.run_id)
    assert refreshed_completed is not None and refreshed_completed.status == "completed"


async def test_sweep_with_no_stale_runs_returns_zero(mongo_db):  # noqa: ARG001
    fresh = _make_run(status="running", created_minutes_ago=2)
    await fresh.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 0


async def test_sweep_accepts_older_than_seconds(mongo_db, fake_transport):  # noqa: ARG001
    """Worker boot uses a short cutoff — `older_than_seconds` lets the sweep
    pick up runs orphaned by a worker crash that happened seconds ago."""
    just_now = ChatRunDoc(
        run_id="r-fresh",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c-fresh",
        user_message_id="um1",
        status="running",  # type: ignore[arg-type]
        createdAt=datetime.now(UTC),
    )
    seconds_old = ChatRunDoc(
        run_id="r-30s",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c-30s",
        user_message_id="um1",
        status="running",  # type: ignore[arg-type]
        createdAt=datetime.now(UTC) - timedelta(seconds=30),
    )
    await just_now.insert()
    await seconds_old.insert()

    n = await sweeper.sweep_stale_runs(older_than_seconds=5)

    assert n == 1
    refreshed_old = await ChatRunDoc.find_one(ChatRunDoc.run_id == seconds_old.run_id)
    assert refreshed_old is not None and refreshed_old.status == "interrupted"
    refreshed_fresh = await ChatRunDoc.find_one(ChatRunDoc.run_id == just_now.run_id)
    assert refreshed_fresh is not None and refreshed_fresh.status == "running"


async def test_sweep_appends_interrupted_event_when_stream_exists(
    mongo_db,  # noqa: ARG001
    fake_transport,
):
    """Live SSE subscribers should get a terminal `interrupted` frame instead
    of timing out on heartbeats."""
    stale = _make_run(status="running", created_minutes_ago=30)
    await stale.insert()
    # A live stream exists for this run (the previous process wrote events
    # before crashing). After the sweep, the latest event must be `interrupted`.
    await fake_transport.append_event(stale.run_id, "chunk", {"content": "partial"})

    await sweeper.sweep_stale_runs(older_than_minutes=10)

    events = [e async for e in fake_transport.read_events(stale.run_id, after="0", block_ms=10)]
    assert events[-1].event == "interrupted"
    assert events[-1].is_terminal


async def test_sweep_skips_transport_when_no_stream(
    mongo_db,  # noqa: ARG001
    fake_transport,
):
    """Runs that died before writing any event have no stream — the sweep
    must not blindly create one."""
    stale = _make_run(status="running", created_minutes_ago=30)
    await stale.insert()

    await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert await fake_transport.stream_exists(stale.run_id) is False


# --- review-finding fixes ---------------------------------------------------


async def test_sweep_older_than_minutes_zero_is_honored(mongo_db, fake_transport):  # noqa: ARG001
    """Regression for review finding #2 — `older_than_minutes=0` was being
    coerced to 10 by `or 10`, silently nullifying an emergency 'sweep
    everything queued/running right now' call."""
    just_now = ChatRunDoc(
        run_id="r-now",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c-now",
        user_message_id="um1",
        status="running",  # type: ignore[arg-type]
        createdAt=datetime.now(UTC) - timedelta(seconds=1),
    )
    await just_now.insert()

    n = await sweeper.sweep_stale_runs(older_than_minutes=0)

    assert n == 1
    refreshed = await ChatRunDoc.find_one(ChatRunDoc.run_id == just_now.run_id)
    assert refreshed is not None and refreshed.status == "interrupted"


async def test_sweep_rejects_both_cutoff_kwargs(mongo_db):  # noqa: ARG001
    """Review finding #9 — passing both `older_than_minutes` and
    `older_than_seconds` used to silently ignore minutes. Now an explicit
    error so the caller's intent is unambiguous."""
    with pytest.raises(ValueError, match="exactly one"):
        await sweeper.sweep_stale_runs(older_than_minutes=10, older_than_seconds=5)


async def test_sweep_no_redis_env_uses_memory_transport(
    mongo_db,  # noqa: ARG001
    monkeypatch,
    caplog,
):
    """When POCKETPAW_REDIS_URL is unset the sweeper still works — the
    transport selector falls back to the in-memory impl. The sweep must
    not raise or log a warning on the tick."""
    import logging

    from pocketpaw_ee.cloud.chat.runs import transport as transport_mod

    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    monkeypatch.delenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", raising=False)
    transport_mod._reset_for_tests()

    stale = _make_run(status="running", created_minutes_ago=30)
    await stale.insert()

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.chat.runs.sweeper"):
        n = await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert n == 1
    sweeper_warnings = [
        r
        for r in caplog.records
        if r.name == "pocketpaw_ee.cloud.chat.runs.sweeper" and r.levelno >= logging.WARNING
    ]
    assert sweeper_warnings == []
    transport_mod._reset_for_tests()


async def test_sweep_sets_ttl_on_resurrected_stream(mongo_db, fake_transport, monkeypatch):  # noqa: ARG001
    """Review finding #8 — TOCTOU window between `stream_exists` and
    `append_event`. The append on a TTL-evicted stream creates a brand-new
    key, so the sweep must set a TTL so the orphan doesn't live forever."""
    stale = _make_run(status="running", created_minutes_ago=30)
    await stale.insert()

    set_ttls: list[tuple[str, int]] = []
    orig_set_ttl = fake_transport.set_ttl

    async def _record_set_ttl(run_id: str, ttl_seconds: int) -> None:
        set_ttls.append((run_id, ttl_seconds))
        await orig_set_ttl(run_id, ttl_seconds)

    # Simulate a stream that exists at check time (race window).
    await fake_transport.append_event(stale.run_id, "chunk", {"content": "p"})
    monkeypatch.setattr(fake_transport, "set_ttl", _record_set_ttl)

    await sweeper.sweep_stale_runs(older_than_minutes=10)

    assert any(rid == stale.run_id and ttl > 0 for rid, ttl in set_ttls), (
        f"sweep should set a TTL on the run's stream, got {set_ttls!r}"
    )
