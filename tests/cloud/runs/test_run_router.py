"""Tests for the run streaming + stop endpoints."""

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio


async def test_stream_replays_buffered_events(runs_app_client, seed_run, monkeypatch):  # noqa: ARG001
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.router.get_stream_transport", lambda: transport
    )
    await transport.append_event("r1", "chunk", {"content": "hi"})
    await transport.append_event("r1", "stream_end", {"assistant_message_id": "m2"})

    resp = await runs_app_client.get("/cloud/chat/runs/r1/stream?after=0")
    body = resp.text
    assert "event: chunk" in body
    assert "event: stream_end" in body
    assert "id: " in body


async def test_stop_sets_cancel_flag(runs_app_client, seed_run, monkeypatch):  # noqa: ARG001
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.router.get_stream_transport", lambda: transport
    )
    resp = await runs_app_client.post("/cloud/chat/runs/r1/stop")
    assert resp.status_code == 200
    assert await transport.is_cancelled("r1") is True


async def test_stream_unauthorized_for_other_workspace(runs_app_client, mongo_db, monkeypatch):  # noqa: ARG001
    """A run from another workspace must 404 the caller, not leak data."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.router.get_stream_transport",
        lambda: RedisStreamTransport(redis),
    )
    # No seed_run -> r1 doesn't exist for w1
    resp = await runs_app_client.get("/cloud/chat/runs/r1/stream?after=0")
    assert resp.status_code == 404
