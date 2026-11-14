# tests/ee/sites/test_admission_lua.py — runs ``admission``'s Redis lease scripts for
# real, on fakeredis with Lua (the ``fakeredis[lua]`` dev extra, which embeds lupa), so
# CI covers the script itself and not only the Python around it.
#
# Pinned here: a lease under the budget is granted, one over it waits in the queue,
# releasing a lease lets the queue head in, a crashed holder's lease (and a dead
# waiter's queue slot) expires on its own, the refresh script extends only a live
# lease, and many concurrent ``memory_lease`` holders never exceed the budget.
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pocketpaw_ee.sites import admission

BUDGET = 10
LEASE_TTL_MS = 60_000


@pytest.fixture(autouse=True)
def _budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(admission.BUDGET_ENV, str(BUDGET))
    yield
    admission.use_redis(None)


@pytest.fixture
async def redis() -> Any:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def _acquire(
    redis: Any,
    lease_id: str,
    gib: int,
    *,
    lease_ttl_ms: int = LEASE_TTL_MS,
    waiter_ttl_ms: int = 30_000,
) -> bool:
    """One run of the real acquire script; True when it granted the lease."""
    granted = await redis.eval(
        admission._ACQUIRE_LUA,
        4,
        admission._LEASES_KEY,
        admission._GIB_KEY,
        admission._QUEUE_KEY,
        admission._HEARTBEAT_KEY,
        lease_id,
        gib,
        BUDGET,
        lease_ttl_ms,
        waiter_ttl_ms,
    )
    return int(granted) == 1


async def _held(redis: Any) -> dict[str, int]:
    raw = await redis.hgetall(admission._GIB_KEY)
    return {k.decode(): int(v) for k, v in raw.items()}


async def _queued(redis: Any) -> list[str]:
    return [m.decode() for m in await redis.zrange(admission._QUEUE_KEY, 0, -1)]


class TestAcquireScript:
    async def test_admits_while_the_budget_has_room(self, redis) -> None:
        assert await _acquire(redis, "a", 4)
        assert await _acquire(redis, "b", 4)
        assert await _held(redis) == {"a": 4, "b": 4}
        assert await _queued(redis) == []

    async def test_refuses_over_the_budget_and_queues_the_waiter(self, redis) -> None:
        assert await _acquire(redis, "a", 4)
        assert await _acquire(redis, "b", 4)
        assert not await _acquire(redis, "c", 4)
        assert await _held(redis) == {"a": 4, "b": 4}
        assert await _queued(redis) == ["c"]
        # Polling again does not grant it or queue it twice.
        assert not await _acquire(redis, "c", 4)
        assert await _queued(redis) == ["c"]

    async def test_a_request_bigger_than_the_budget_runs_alone(self, redis) -> None:
        assert await _acquire(redis, "big", BUDGET * 2)
        assert not await _acquire(redis, "small", 1)

    async def test_release_lets_the_queue_head_in(self, redis) -> None:
        assert await _acquire(redis, "a", 4)
        assert await _acquire(redis, "b", 4)
        assert not await _acquire(redis, "c", 4)
        await admission._redis_release(redis, "a", None)
        assert await _held(redis) == {"b": 4}
        assert await _acquire(redis, "c", 4)
        assert await _held(redis) == {"b": 4, "c": 4}
        assert await _queued(redis) == []

    async def test_fifo_a_small_request_does_not_jump_a_big_one(self, redis) -> None:
        assert await _acquire(redis, "a", 8)
        assert not await _acquire(redis, "big", 4)  # head of the queue
        assert not await _acquire(redis, "small", 1)  # would fit, but is not the head
        assert await _queued(redis) == ["big", "small"]

    async def test_a_crashed_holders_lease_expires(self, redis) -> None:
        # The holder never releases (its worker died); only the TTL frees its memory.
        assert await _acquire(redis, "crashed", BUDGET, lease_ttl_ms=50)
        assert not await _acquire(redis, "next", 4)
        await asyncio.sleep(0.1)
        assert await _acquire(redis, "next", 4)
        assert await _held(redis) == {"next": 4}

    async def test_a_dead_waiter_drops_out_of_the_queue(self, redis) -> None:
        assert await _acquire(redis, "a", 8)
        assert not await _acquire(redis, "dead", 4, waiter_ttl_ms=50)  # stops polling
        assert not await _acquire(redis, "live", 1)
        await asyncio.sleep(0.1)
        # The dead head's heartbeat lapsed, so the live waiter is now the head and fits.
        assert await _acquire(redis, "live", 1)
        assert "dead" not in await _queued(redis)


class TestRefreshScript:
    async def test_extends_a_held_lease(self, redis) -> None:
        assert await _acquire(redis, "a", 4, lease_ttl_ms=1_000)
        before = await redis.zscore(admission._LEASES_KEY, "a")
        changed = await redis.eval(admission._REFRESH_LUA, 1, admission._LEASES_KEY, "a", 60_000)
        assert int(changed) == 1
        assert await redis.zscore(admission._LEASES_KEY, "a") > before

    async def test_never_resurrects_an_expired_lease(self, redis) -> None:
        assert await _acquire(redis, "a", 4, lease_ttl_ms=50)
        await asyncio.sleep(0.1)
        assert await _acquire(redis, "b", 1)  # purges the expired "a"
        changed = await redis.eval(admission._REFRESH_LUA, 1, admission._LEASES_KEY, "a", 60_000)
        assert int(changed) == 0
        assert await redis.zscore(admission._LEASES_KEY, "a") is None


class TestMemoryLeaseOverRedis:
    async def test_concurrent_admits_never_exceed_the_budget(self, redis, monkeypatch) -> None:
        monkeypatch.setattr(admission, "_POLL_SECONDS", 0.01)
        admission.use_redis(redis)
        in_use = peak = 0
        admitted: list[bool] = []

        async def build(gib: int) -> None:
            nonlocal in_use, peak
            async with admission.memory_lease(gib, max_wait_seconds=10) as ok:
                admitted.append(ok)
                in_use += gib
                peak = max(peak, in_use)
                held = sum((await _held(redis)).values())
                assert held <= BUDGET
                await asyncio.sleep(0.02)
                in_use -= gib

        await asyncio.gather(*(build(gib) for gib in [4, 2, 4, 4, 2, 4, 2, 4]))
        assert admitted == [True] * 8
        assert peak <= BUDGET
        assert await _held(redis) == {}
        assert await _queued(redis) == []

    async def test_a_waiting_lease_is_visible_by_tag_until_admitted(
        self, redis, monkeypatch
    ) -> None:
        monkeypatch.setattr(admission, "_POLL_SECONDS", 0.01)
        admission.use_redis(redis)
        assert await _acquire(redis, "holder", BUDGET)

        async def waiter() -> bool:
            async with admission.memory_lease(4, wait_tag="job-1", max_wait_seconds=5) as ok:
                return ok

        task = asyncio.create_task(waiter())
        deadline = time.monotonic() + 3
        while not await admission.is_waiting(redis, "job-1"):
            assert time.monotonic() < deadline, "the waiting key was never set"
            await asyncio.sleep(0.01)
        await admission._redis_release(redis, "holder", None)
        assert await task is True
        assert not await admission.is_waiting(redis, "job-1")
