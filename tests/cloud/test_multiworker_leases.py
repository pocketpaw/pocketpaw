"""Single-runner leases for background loops, with contenders in one test.

Each contender is a ``Lease`` (or a ``Leased`` start/stop pair) on the real
local Redis under a unique key prefix, standing in for one web process. What is
pinned:

- of two contenders exactly one runs the loop, renewal keeps it there, and
  stopping the holder hands the loop to the other within a renewal interval;
- when Redis is unreachable no contender runs, and a holder that loses Redis
  stops its loop once its deadline passes;
- ``per_host`` leases are keyed by hostname;
- with the multi-worker switch off, ``leased`` runs the loop directly and
  touches no Redis;
- the run sweeper skips the cluster sweeps when another process holds the
  sweep lease, boot pass included, and still runs the per-host jail GC;
- mandate autopilot: a change announced by another process starts the loop on
  the holder and stops it everywhere else.

Mutations: ``tests/mutations/multiworker_safety.json``. Skipped when no Redis
answers on localhost:6379 (CI has no Redis service).
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from pocketpaw_ee.cloud._core import lease as lease_mod
from pocketpaw_ee.cloud._core.lease import Lease, leased
from pocketpaw_ee.cloud._core.realtime import broadcast

pytestmark = pytest.mark.asyncio

TTL_MS = 600


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("no Redis on localhost:6379")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def prefix(redis, monkeypatch):
    name = f"test:lease:{uuid.uuid4().hex}:"
    monkeypatch.setattr(lease_mod, "LEASE_PREFIX", name)
    yield name
    keys = [k async for k in redis.scan_iter(match=f"{name}*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture(autouse=True)
def _reset():
    broadcast._reset_for_tests()
    yield
    broadcast._reset_for_tests()


class _Loop:
    """A stand-in background loop that records whether it is running."""

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.running = True
        self.starts += 1

    async def stop(self) -> None:
        self.running = False
        self.stops += 1


def _contender(redis, loop: _Loop, name: str = "job", **kw) -> Lease:
    return Lease(name, ttl_ms=TTL_MS, redis=redis, on_acquired=loop.start, on_lost=loop.stop, **kw)


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for predicate")


class _Flaky:
    """A Redis client that can be switched off mid-test."""

    def __init__(self, real) -> None:
        self.real = real
        self.down = False

    async def set(self, *a, **kw):
        if self.down:
            raise ConnectionError("redis down")
        return await self.real.set(*a, **kw)

    async def eval(self, *a, **kw):
        if self.down:
            raise ConnectionError("redis down")
        return await self.real.eval(*a, **kw)


# --- Lease ----------------------------------------------------------------------


async def test_only_one_of_two_contenders_runs_the_loop(redis, prefix):
    loop_a, loop_b = _Loop(), _Loop()
    a, b = _contender(redis, loop_a), _contender(redis, loop_b)
    await a.start()
    await b.start()
    try:
        await asyncio.sleep(TTL_MS / 1000)  # several renewal rounds
        assert [loop_a.running, loop_b.running].count(True) == 1
        assert loop_a.starts + loop_b.starts == 1
    finally:
        await a.stop()
        await b.stop()


async def test_renewal_keeps_the_lease_past_its_ttl(redis, prefix):
    loop_a, loop_b = _Loop(), _Loop()
    a = _contender(redis, loop_a)
    await a.start()
    b = _contender(redis, loop_b)
    await b.start()
    try:
        await asyncio.sleep(TTL_MS / 1000 * 3)
        assert loop_a.running and a.held
        assert loop_a.stops == 0
        assert not loop_b.running and loop_b.starts == 0
    finally:
        await a.stop()
        await b.stop()


async def test_stopping_the_holder_hands_the_loop_over(redis, prefix):
    loop_a, loop_b = _Loop(), _Loop()
    a = _contender(redis, loop_a)
    await a.start()
    b = _contender(redis, loop_b)
    await b.start()
    try:
        assert loop_a.running and not loop_b.running
        await a.stop()
        assert not loop_a.running and loop_a.stops == 1
        # Released, not expired: B takes it on its next renewal tick.
        await _until(lambda: loop_b.running, timeout=TTL_MS / 1000)
    finally:
        await b.stop()


async def test_no_contender_runs_while_redis_is_down(redis, prefix):
    flaky = _Flaky(redis)
    flaky.down = True
    loop = _Loop()
    a = _contender(flaky, loop)
    await a.start()
    try:
        await asyncio.sleep(TTL_MS / 1000)
        assert loop.starts == 0 and not a.held
    finally:
        await a.stop()


async def test_holder_stops_its_loop_when_redis_goes_down(redis, prefix):
    flaky = _Flaky(redis)
    loop = _Loop()
    a = _contender(flaky, loop)
    await a.start()
    try:
        assert loop.running
        flaky.down = True
        await _until(lambda: not loop.running, timeout=TTL_MS / 1000 * 2)
        assert not a.held
        # ... and resumes once Redis is back and the key is free again.
        flaky.down = False
        await _until(lambda: loop.running, timeout=TTL_MS / 1000 * 3)
    finally:
        await a.stop()


async def test_a_stalled_holder_cannot_release_its_successors_lease(redis, prefix):
    loop_a, loop_b = _Loop(), _Loop()
    a = _contender(redis, loop_a)
    assert await a.try_acquire()
    # A stalls past its ttl; B takes the expired key.
    await redis.delete(a.key)
    b = _contender(redis, loop_b)
    assert await b.try_acquire()

    await a.stop()  # A's release must not delete B's key

    assert await redis.get(b.key) == b.owner


async def test_per_host_lease_is_keyed_by_hostname(redis, prefix):
    a = Lease("jails", redis=redis, per_host=True)
    assert a.key == f"{prefix}jails:{socket.gethostname()}"


# --- leased ---------------------------------------------------------------------


async def test_leased_with_the_switch_off_runs_directly_without_redis(monkeypatch):
    def _no_redis():
        raise AssertionError("single-process mode must not touch Redis")

    monkeypatch.setattr(lease_mod, "get_redis", _no_redis)
    loop = _Loop()
    pair = leased("job", loop.start, loop.stop)

    await pair.start()
    assert loop.running and pair.held
    await pair.stop()
    assert not loop.running


async def test_leased_with_the_switch_on_runs_on_one_process(redis, prefix):
    broadcast.configure(enabled=True)
    loop_a, loop_b = _Loop(), _Loop()
    pair_a = leased("job", loop_a.start, loop_a.stop, ttl_ms=TTL_MS, redis=redis)
    pair_b = leased("job", loop_b.start, loop_b.stop, ttl_ms=TTL_MS, redis=redis)
    await pair_a.start()
    await pair_b.start()
    try:
        assert loop_a.running and not loop_b.running
        assert pair_a.held and not pair_b.held
    finally:
        await pair_a.stop()
        await pair_b.stop()


# --- the run sweeper ------------------------------------------------------------


async def test_sweeper_skips_cluster_sweeps_when_another_process_holds_the_lease(
    redis, prefix, monkeypatch
):
    from pocketpaw_ee import extensions

    calls: list[str] = []

    async def cluster_sweep():
        calls.append("cluster")

    async def jail_sweep():
        calls.append("jail")

    monkeypatch.setattr(extensions, "_sweeps", lambda: ([cluster_sweep], [jail_sweep]))
    monkeypatch.setattr(lease_mod, "get_redis", lambda: redis)
    monkeypatch.setattr(extensions, "_sweeper_loop", lambda: asyncio.sleep(3600))
    broadcast.configure(enabled=True)
    # Another process already holds the cluster sweep lease.
    await redis.set(f"{prefix}run_sweeper", "someone-else", px=60_000)

    await extensions.start_run_sweeper()
    try:
        assert calls == ["jail"]  # the boot pass ran only the per-host sweep
        calls.clear()
        await extensions._run_sweeps()  # a later tick: same
        assert calls == ["jail"]
    finally:
        await extensions.stop_run_sweeper()


async def test_sweeper_runs_everything_when_it_holds_the_leases(redis, prefix, monkeypatch):
    from pocketpaw_ee import extensions

    calls: list[str] = []

    async def cluster_sweep():
        calls.append("cluster")

    async def jail_sweep():
        calls.append("jail")

    monkeypatch.setattr(extensions, "_sweeps", lambda: ([cluster_sweep], [jail_sweep]))
    monkeypatch.setattr(lease_mod, "get_redis", lambda: redis)
    monkeypatch.setattr(extensions, "_sweeper_loop", lambda: asyncio.sleep(3600))
    broadcast.configure(enabled=True)

    await extensions.start_run_sweeper()
    try:
        assert calls == ["cluster", "jail"]
    finally:
        await extensions.stop_run_sweeper()


# --- mandate autopilot ----------------------------------------------------------


class _AutopilotSpy:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def start(self, workspace_id, mandate_id, users, *, run_immediate=True):
        self.started.append(mandate_id)

    async def stop(self, mandate_id):
        self.stopped.append(mandate_id)


@pytest.fixture
def autopilot(monkeypatch):
    from pocketpaw_ee.cloud.mandates import autopilot as mod
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    spy = _AutopilotSpy()
    monkeypatch.setattr(mod, "start_autopilot", spy.start)
    monkeypatch.setattr(mod, "stop_autopilot", spy.stop)

    async def rows():
        return [{"workspace_id": "w1", "mandate_id": "m-on", "users": 3}]

    monkeypatch.setattr(mandate_service, "list_autopilot_enabled", rows)
    monkeypatch.setattr(mod, "_singleton", None)
    return mod, spy


async def test_autopilot_change_starts_the_loop_on_the_holder(autopilot, monkeypatch):
    mod, spy = autopilot
    monkeypatch.setattr(mod, "runs_here", lambda: True)

    await mod._on_remote_change("m-on")
    await mod._on_remote_change("m-off")

    assert spy.started == ["m-on"]
    assert spy.stopped == ["m-off"]


async def test_autopilot_change_stops_the_loop_on_a_non_holder(autopilot, monkeypatch):
    mod, spy = autopilot
    monkeypatch.setattr(mod, "runs_here", lambda: False)

    await mod._on_remote_change("m-on")

    assert spy.started == []
    assert spy.stopped == ["m-on"]


async def test_autopilot_change_is_registered_on_the_broadcast(autopilot):
    mod, _spy = autopilot
    assert broadcast._INVALIDATORS[mod._CHANGE_INVALIDATOR] is mod._on_remote_change


async def test_autopilot_runs_here_follows_the_lease(autopilot, redis, prefix):
    mod, _spy = autopilot
    assert mod.runs_here() is True  # no lease: single process

    broadcast.configure(enabled=True)
    await redis.set(f"{prefix}{mod.AUTOPILOT_LEASE}", "someone-else", px=60_000)
    pair = mod.autopilot_singleton()
    pair._redis = redis
    await pair.start()
    try:
        assert mod.runs_here() is False
    finally:
        await pair.stop()
