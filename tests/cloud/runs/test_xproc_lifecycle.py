"""Lifecycle wiring for the xproc bridge: worker pins its role on _startup,
the web side starts the consumer when POCKETPAW_REDIS_URL is set and is a
no-op when it isn't (Tier 0 deployments must not log Redis errors)."""

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee import extensions as ext
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.runs import worker

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_xproc():
    xproc._reset_for_tests()
    yield
    xproc._reset_for_tests()


async def test_worker_startup_pins_worker_role(monkeypatch):
    """``_startup`` must call ``set_role('worker')`` before init_realtime,
    otherwise the bus singleton on this process becomes the destination for
    emits that should cross the bridge."""

    async def _noop_db(_uri):
        return None

    def _noop_realtime():
        return None

    async def _noop_sweep(**kwargs):
        return 0

    monkeypatch.setattr(worker, "init_cloud_db", _noop_db)
    monkeypatch.setattr(worker, "init_realtime", _noop_realtime)
    monkeypatch.setattr(worker, "sweep_stale_runs", _noop_sweep)

    assert xproc.is_worker() is False
    await worker._startup({})
    assert xproc.is_worker() is True


async def test_start_xproc_consumer_noop_without_redis_env(monkeypatch):
    """A cloud deploy that hasn't enabled Tier 1/2 (no POCKETPAW_REDIS_URL)
    must not spin up the consumer — it would just log Redis errors forever."""
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)

    await ext.start_xproc_consumer()

    assert ext._xproc_consumer_task is None


async def test_start_xproc_consumer_spawns_task_when_redis_set(monkeypatch):
    """With POCKETPAW_REDIS_URL set, the consumer task is created and stop
    cancels it cleanly."""
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")

    spawned: list = []

    async def _fake_run_consumer(**kwargs):
        spawned.append("started")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            spawned.append("cancelled")
            raise

    monkeypatch.setattr("pocketpaw_ee.cloud._core.realtime.xproc.run_consumer", _fake_run_consumer)

    await ext.start_xproc_consumer()
    assert ext._xproc_consumer_task is not None
    assert not ext._xproc_consumer_task.done()
    # Let the task actually start before we cancel it.
    await asyncio.sleep(0.01)
    assert spawned == ["started"]

    await ext.stop_xproc_consumer()
    assert ext._xproc_consumer_task is None
    assert spawned == ["started", "cancelled"]


async def test_stop_xproc_consumer_safe_when_never_started():
    """Idempotent stop — Tier 0 deploys never start the task; shutdown still
    runs stop_xproc_consumer."""
    ext._xproc_consumer_task = None
    await ext.stop_xproc_consumer()
    assert ext._xproc_consumer_task is None
