# tests/test_agent_pool_stale_rebuild.py: concurrent runs rebuild a stale agent once.
#
# When an agent's config changed, ``AgentPool.get`` tears the cached instance down
# and builds a new one. Several runs for the same agent arriving together (a busy
# queue releasing a batch) must share ONE rebuild: every caller gets the live
# instance, nothing is built and then dropped from the cache unstopped, and no
# caller is handed an instance that was already torn down.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import AgentInstance, AgentPool

pytestmark = pytest.mark.asyncio

AID = "0123456789abcdef01234567"


def _instance(updated_at: datetime) -> AgentInstance:
    return AgentInstance(
        agent_id=AID,
        agent_name="Ripple",
        config={},
        backend=SimpleNamespace(),
        soul_manager=None,
        memory_namespace="ns",
        created_from_updated_at=updated_at,
    )


async def test_concurrent_gets_on_a_stale_agent_rebuild_once(monkeypatch):
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    doc = SimpleNamespace(disabled=False, updatedAt=t0 + timedelta(minutes=1))

    class _Model:
        async def get(self, oid):  # noqa: ARG002
            await asyncio.sleep(0)
            return doc

    monkeypatch.setattr("pocketpaw.agents.pool._resolve_agent_model", lambda: _Model())

    pool = AgentPool()
    stale = _instance(t0)
    pool._instances[AID] = stale
    built: list[AgentInstance] = []
    torn: list[AgentInstance] = []

    async def _build(agent_doc):
        await asyncio.sleep(0.01)
        inst = _instance(agent_doc.updatedAt)
        built.append(inst)
        pool._instances[AID] = inst
        return inst

    async def _teardown(inst):
        await asyncio.sleep(0.01)
        torn.append(inst)

    monkeypatch.setattr(pool, "_build", _build)
    monkeypatch.setattr(pool, "_teardown", _teardown)

    got = await asyncio.gather(*(pool.get(AID) for _ in range(3)))

    assert len(built) == 1, f"rebuilt {len(built)} times"
    assert torn == [stale]
    assert all(g is built[0] for g in got)
    assert pool._instances[AID] is built[0]


async def test_a_run_that_starts_while_get_waits_for_the_lock_is_not_torn_down(monkeypatch):
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    doc = SimpleNamespace(disabled=False, updatedAt=t0 + timedelta(minutes=1))

    class _Model:
        async def get(self, oid):  # noqa: ARG002
            return doc

    monkeypatch.setattr("pocketpaw.agents.pool._resolve_agent_model", lambda: _Model())

    pool = AgentPool()
    stale = _instance(t0)
    pool._instances[AID] = stale
    torn: list[AgentInstance] = []

    async def _teardown(inst):
        torn.append(inst)

    async def _build(agent_doc):  # pragma: no cover - must not be reached
        raise AssertionError("rebuilt an instance with a run in flight")

    monkeypatch.setattr(pool, "_teardown", _teardown)
    monkeypatch.setattr(pool, "_build", _build)

    await pool._build_lock.acquire()
    pending = asyncio.create_task(pool.get(AID))
    await asyncio.sleep(0.01)  # get saw the stale config and waits on the lock
    stale.active_runs = 1  # a run started on the cached instance meanwhile
    pool._build_lock.release()

    assert await pending is stale
    assert torn == []
