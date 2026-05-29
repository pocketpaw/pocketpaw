"""Tier 2 arq worker: ``execute_run_job`` rehydrates a ``RunSpec`` and
delegates to ``execute_run``; ``_startup`` sweeps runs orphaned by the
previous worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.chat.runs import worker
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

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


async def test_execute_run_job_calls_execute_run(monkeypatch):
    seen: list[str] = []

    async def fake_execute_run(spec):
        seen.append(spec.run_id)

    monkeypatch.setattr(worker, "execute_run", fake_execute_run)

    await worker.execute_run_job({}, _spec().model_dump())

    assert seen == ["r1"]


async def test_execute_run_job_rehydrates_spec_from_dict(monkeypatch):
    received: list[RunSpec] = []

    async def fake_execute_run(spec):
        received.append(spec)

    monkeypatch.setattr(worker, "execute_run", fake_execute_run)

    await worker.execute_run_job({}, _spec("r2").model_dump())

    assert isinstance(received[0], RunSpec)
    assert received[0].run_id == "r2"
    assert received[0].workspace_id == "w1"


async def test_startup_runs_short_cutoff_sweep(mongo_db, monkeypatch):  # noqa: ARG001
    """The boot sweep must catch a run orphaned a few seconds ago by the
    previous worker — the 10-minute default cutoff would leave it visible
    until the next heartbeat tick."""

    # Stub the DB + realtime init since the test fixture already initialised
    # Beanie and the realtime bus is not under test here.
    async def _noop_db(_uri):
        return None

    def _noop_realtime():
        return None

    monkeypatch.setattr(worker, "init_cloud_db", _noop_db)
    monkeypatch.setattr(worker, "init_realtime", _noop_realtime)
    monkeypatch.setenv(worker._BOOT_SWEEP_ENV, "true")

    orphan = ChatRunDoc(
        run_id="r-orphan",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c-orphan",
        user_message_id="um1",
        status="running",  # type: ignore[arg-type]
        createdAt=datetime.now(UTC) - timedelta(seconds=30),
    )
    await orphan.insert()

    await worker._startup({})

    refreshed = await ChatRunDoc.find_one(ChatRunDoc.run_id == orphan.run_id)
    assert refreshed is not None and refreshed.status == "interrupted"


async def test_startup_skips_sweep_when_gate_off(mongo_db, monkeypatch):  # noqa: ARG001
    """Default off: a fresh worker booting alongside a healthy sibling must
    NOT mark the sibling's in-flight runs as interrupted."""

    async def _noop_db(_uri):
        return None

    def _noop_realtime():
        return None

    monkeypatch.setattr(worker, "init_cloud_db", _noop_db)
    monkeypatch.setattr(worker, "init_realtime", _noop_realtime)
    monkeypatch.delenv(worker._BOOT_SWEEP_ENV, raising=False)

    inflight = ChatRunDoc(
        run_id="r-sibling",
        workspace="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        user_id="u1",
        agent_id="a1",
        client_message_id="c-sibling",
        user_message_id="um1",
        status="running",  # type: ignore[arg-type]
        createdAt=datetime.now(UTC) - timedelta(seconds=30),
    )
    await inflight.insert()

    await worker._startup({})

    refreshed = await ChatRunDoc.find_one(ChatRunDoc.run_id == inflight.run_id)
    assert refreshed is not None and refreshed.status == "running"


async def test_worker_settings_exposes_execute_run_job():
    assert worker.execute_run_job in worker.WorkerSettings.functions
    assert worker.WorkerSettings.max_tries == 1
    assert worker.WorkerSettings.on_startup is worker._startup
    assert worker.WorkerSettings.on_shutdown is worker._shutdown
