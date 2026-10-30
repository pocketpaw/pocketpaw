# tests/cloud/runs/test_redis_client.py — process-wide Redis clients.
# Changes (2026-10-01, CN-4): get_arq_pool singleton, close_arq_pool reset, the
# four enqueuers sharing it, and CloudLifecycleHook.on_shutdown closing it and
# then the Redis clients (close_redis).

import pytest
from pocketpaw_ee.cloud._core import redis_client


@pytest.mark.asyncio
async def test_get_redis_returns_singleton(monkeypatch):
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    redis_client._reset_for_tests()
    a = redis_client.get_redis()
    b = redis_client.get_redis()
    assert a is b  # same client instance reused


def test_get_redis_without_url_raises(monkeypatch):
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    redis_client._reset_for_tests()
    with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
        redis_client.get_redis()


# --- the process-wide arq pool (CN-4) ---------------------------------------


class _FakeArqPool:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_get_arq_pool_returns_singleton_even_under_concurrency(monkeypatch):
    import asyncio

    built: list[_FakeArqPool] = []

    async def _create_pool(_settings):
        await asyncio.sleep(0)  # yield so concurrent callers race the lock
        built.append(_FakeArqPool())
        return built[-1]

    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(redis_client, "create_pool", _create_pool)
    redis_client._reset_for_tests()
    try:
        pools = await asyncio.gather(*(redis_client.get_arq_pool() for _ in range(5)))
        assert len(built) == 1
        assert all(p is built[0] for p in pools)
        assert await redis_client.get_arq_pool() is built[0]
    finally:
        redis_client._reset_for_tests()


@pytest.mark.asyncio
async def test_get_arq_pool_without_url_raises(monkeypatch):
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    redis_client._reset_for_tests()
    with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
        await redis_client.get_arq_pool()


@pytest.mark.asyncio
async def test_close_arq_pool_closes_and_resets(monkeypatch):
    first, second = _FakeArqPool(), _FakeArqPool()
    queue = [first, second]

    async def _create_pool(_settings):
        return queue.pop(0)

    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(redis_client, "create_pool", _create_pool)
    redis_client._reset_for_tests()
    try:
        assert await redis_client.get_arq_pool() is first
        await redis_client.close_arq_pool()
        assert first.closed == 1
        assert redis_client._arq_pool is None
        # The next caller builds a fresh pool rather than reusing the closed one.
        assert await redis_client.get_arq_pool() is second
    finally:
        redis_client._reset_for_tests()


def test_every_enqueuer_shares_the_one_pool():
    """The four copies this replaced each built their own pool; three were never
    closed. Every enqueuer must resolve to the redis_client getter."""
    from pocketpaw_ee.cloud.chat.runs import arq_executor
    from pocketpaw_ee.cloud.jobs import service as jobs_service
    from pocketpaw_ee.sites import build_job, delete_job

    for mod in (arq_executor, jobs_service, build_job, delete_job):
        assert mod._get_pool is redis_client.get_arq_pool, mod.__name__


@pytest.mark.asyncio
async def test_lifecycle_shutdown_closes_the_arq_pool_and_redis_clients(monkeypatch):
    from pocketpaw_ee.extensions import CloudLifecycleHook

    calls: list[str] = []

    async def _arq() -> None:
        calls.append("arq")

    async def _redis() -> None:
        calls.append("redis")

    monkeypatch.setattr(redis_client, "close_arq_pool", _arq)
    monkeypatch.setattr(redis_client, "close_redis", _redis)
    await CloudLifecycleHook().on_shutdown()
    assert calls == ["arq", "redis"]
