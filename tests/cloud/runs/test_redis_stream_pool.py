# Run-stream reads get their own Redis pool, and run streams carry a TTL from
# the first append.
#
# Why the pool split matters: every open SSE stream parks one connection in
# XREAD BLOCK for up to 15s. On the shared pool, ~120 viewers per process
# exhausted it and then every other Redis user failed with "Too many
# connections" (ws tickets, cancel keys, session revocation, rate limits, arq
# enqueue). The tests below prove the blocking lane is separate, and that
# running it dry leaves the shared pool working.
#
# The real-Redis test needs a server at localhost:6379 and skips without one.

from __future__ import annotations

import asyncio
import uuid

import fakeredis
import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.runs import transport as transport_mod
from pocketpaw_ee.cloud.chat.runs.domain import stream_max_lifetime_seconds
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport, initial_stream_ttl
from redis.asyncio import BlockingConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    redis_client._reset_for_tests()
    transport_mod._reset_for_tests()
    yield
    redis_client._reset_for_tests()
    transport_mod._reset_for_tests()


class TestBlockingClient:
    def test_is_a_separate_client_and_pool(self, clients):
        shared = redis_client.get_redis()
        blocking = redis_client.get_blocking_redis()
        assert blocking is not shared
        assert blocking.connection_pool is not shared.connection_pool
        assert redis_client.get_blocking_redis() is blocking

    def test_waits_for_a_connection_instead_of_failing_instantly(self, clients):
        pool = redis_client.get_blocking_redis().connection_pool
        assert isinstance(pool, BlockingConnectionPool)
        assert pool.timeout is not None and pool.timeout > 0

    def test_read_timeout_outlasts_the_longest_block(self, clients):
        """The stream reader and the xproc consumer both block 15s. A read
        timeout at or under that severs healthy streams."""
        kwargs = redis_client.get_blocking_redis().connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] > 15
        assert kwargs["socket_timeout"] > xproc.XPROC_BLOCK_MS / 1000

    def test_ceiling_is_env_configurable(self, clients, monkeypatch):
        monkeypatch.setenv("POCKETPAW_REDIS_STREAM_MAX_CONNECTIONS", "7")
        assert redis_client.get_blocking_redis().connection_pool.max_connections == 7

    def test_default_ceiling(self, clients, monkeypatch):
        monkeypatch.delenv("POCKETPAW_REDIS_STREAM_MAX_CONNECTIONS", raising=False)
        assert redis_client.get_blocking_redis().connection_pool.max_connections == 512

    def test_without_url_raises(self, monkeypatch):
        monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
        redis_client._reset_for_tests()
        with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
            redis_client.get_blocking_redis()

    @pytest.mark.asyncio
    async def test_close_redis_closes_both(self, clients):
        redis_client.get_redis()
        redis_client.get_blocking_redis()
        await redis_client.close_redis()
        assert redis_client._client is None
        assert redis_client._blocking_client is None


class TestWiring:
    def test_transport_reads_through_the_blocking_client(self, clients, monkeypatch):
        monkeypatch.delenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", raising=False)
        t = transport_mod.get_stream_transport()
        assert isinstance(t, RedisStreamTransport)
        assert t._blocking is redis_client.get_blocking_redis()
        assert t._redis is redis_client.get_redis()

    def test_xproc_consumer_uses_the_blocking_client(self):
        """Source check: the consumer's XREADGROUP BLOCK must not sit on the
        shared pool. Kept to the acquisition line; xproc semantics are owned
        elsewhere."""
        import inspect

        src = inspect.getsource(xproc.run_consumer)
        assert "get_blocking_redis()" in src
        assert "get_redis()" not in src


@pytest.mark.asyncio
async def test_read_events_uses_the_blocking_client_not_the_shared_one():
    server = fakeredis.FakeServer()
    shared = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    blocking = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    calls = {"shared": 0, "blocking": 0}

    def spy(client, name):
        real = client.xread

        async def wrapped(*a, **kw):
            calls[name] += 1
            return await real(*a, **kw)

        client.xread = wrapped

    spy(shared, "shared")
    spy(blocking, "blocking")
    t = RedisStreamTransport(shared, blocking_redis=blocking)
    await t.append_event("r1", "chunk", {"content": "a"})
    await t.append_event("r1", "stream_end", {})
    events = [e async for e in t.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["chunk", "stream_end"]
    assert calls == {"shared": 0, "blocking": 1}


class TestStreamTtl:
    @pytest.fixture
    def redis(self):
        return fakeredis.aioredis.FakeRedis(decode_responses=True)

    def test_initial_ttl_covers_the_run_and_its_retention(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_CLOUD_RUN_STREAM_TTL", "100")
        assert initial_stream_ttl() == stream_max_lifetime_seconds() + 100

    @pytest.mark.asyncio
    async def test_first_append_sets_a_ttl(self, redis):
        t = RedisStreamTransport(redis)
        await t.append_event("r1", "chunk", {"content": "a"})
        ttl = await redis.ttl("run:r1:events")
        assert 0 < ttl <= initial_stream_ttl()
        assert ttl > initial_stream_ttl() - 5

    @pytest.mark.asyncio
    async def test_terminal_ttl_still_shortens_and_later_appends_keep_it(self, redis):
        t = RedisStreamTransport(redis)
        await t.append_event("r1", "chunk", {"content": "a"})
        await t.append_event("r1", "stream_end", {})
        await t.set_ttl("r1", 60)
        assert 0 < await redis.ttl("run:r1:events") <= 60
        # A straggler append (sweeper racing a finished run) must not stretch
        # the terminal TTL back out.
        await t.append_event("r1", "interrupted", {})
        assert 0 < await redis.ttl("run:r1:events") <= 60

    @pytest.mark.asyncio
    async def test_an_expire_failure_does_not_lose_the_event(self, redis, monkeypatch, caplog):
        """Redis < 7 rejects EXPIRE NX. The event must still be written."""
        from redis.asyncio.client import Pipeline

        real_expire = Pipeline.expire

        def bad_expire(self, name, time, nx=False, **kw):
            return real_expire(self, name, "not-a-number", **kw)

        monkeypatch.setattr(Pipeline, "expire", bad_expire)
        t = RedisStreamTransport(redis)
        entry_id = await t.append_event("r1", "chunk", {"content": "a"})
        assert entry_id
        assert await redis.xlen("run:r1:events") == 1


@pytest.mark.asyncio
async def test_exhausting_the_blocking_pool_leaves_the_shared_pool_working(monkeypatch):
    """Real Redis: park the only blocking connection in XREAD BLOCK, then show
    a second stream reader fails cleanly while a shared-pool call succeeds."""
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("POCKETPAW_REDIS_MAX_CONNECTIONS", "2")
    monkeypatch.setenv("POCKETPAW_REDIS_STREAM_MAX_CONNECTIONS", "1")
    monkeypatch.setattr(redis_client, "_BLOCKING_POOL_WAIT_SECONDS", 0.3)
    redis_client._reset_for_tests()
    shared = redis_client.get_redis()
    blocking = redis_client.get_blocking_redis()
    try:
        await asyncio.wait_for(shared.ping(), 2)
    except Exception:
        await redis_client.close_redis()
        pytest.skip("no Redis at localhost:6379")

    key = f"test:w1r:{uuid.uuid4().hex}"
    t = RedisStreamTransport(shared, blocking_redis=blocking)
    parked = asyncio.create_task(blocking.xread({key: "$"}, block=5000))
    try:
        await asyncio.sleep(0.2)  # let the parked read take the only connection
        with pytest.raises(RedisConnectionError):
            async for _ in t.read_events(key, block_ms=1000):
                pass
        # Shared lane untouched: plain commands and appends still work.
        assert await shared.ping()
        await shared.set(f"{key}:probe", "1", ex=30)
        assert await shared.get(f"{key}:probe") == "1"
    finally:
        parked.cancel()
        try:
            await parked
        except BaseException:
            pass
        await shared.delete(key, f"{key}:probe")
        await redis_client.close_redis()
        redis_client._reset_for_tests()
