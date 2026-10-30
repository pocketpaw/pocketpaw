import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.chat.runs.transport import RunStreamTransport


@pytest.fixture
def transport() -> RedisStreamTransport:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisStreamTransport(redis)


def test_redis_impl_satisfies_protocol(transport):
    assert isinstance(transport, RunStreamTransport)  # runtime_checkable


@pytest.mark.asyncio
async def test_append_then_read_replays_all(transport):
    await transport.append_event("r1", "chunk", {"content": "a"})
    await transport.append_event("r1", "chunk", {"content": "b"})
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [(e.event, e.data["content"]) for e in events] == [("chunk", "a"), ("chunk", "b")]


@pytest.mark.asyncio
async def test_read_after_cursor_skips_seen(transport):
    id1 = await transport.append_event("r1", "chunk", {"content": "a"})
    await transport.append_event("r1", "chunk", {"content": "b"})
    events = [e async for e in transport.read_events("r1", after=id1, block_ms=10)]
    assert [e.data["content"] for e in events] == ["b"]


@pytest.mark.asyncio
async def test_read_stops_on_terminal_event(transport):
    await transport.append_event("r1", "chunk", {"content": "a"})
    await transport.append_event("r1", "stream_end", {"assistant_message_id": "m1"})
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "stream_end"
    assert events[-1].is_terminal


@pytest.mark.asyncio
async def test_cancel_flag(transport):
    assert await transport.is_cancelled("r1") is False
    await transport.request_cancel("r1")
    assert await transport.is_cancelled("r1") is True
