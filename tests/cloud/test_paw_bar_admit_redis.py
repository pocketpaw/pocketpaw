# Paw Bar admission through Redis (pocketpaw_ee.paw_bar.admit).
#
# The Lua window tests need a real Redis on localhost:6379 and skip without one
# (fakeredis cannot run Lua here). The routing tests (no Redis configured, Redis
# erroring, the back-off) use stand-ins and run everywhere.

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import redis.asyncio as aioredis
from pocketpaw_ee.paw_bar import admit as admit_mod

from pocketpaw.paw_bar.models import PawBarEvent
from pocketpaw.paw_bar.store import PawBarStore


@pytest.fixture
async def redis():
    client = aioredis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("no Redis on localhost:6379")
    yield client
    await client.aclose()


@pytest.fixture
async def prefix(redis, monkeypatch):
    name = f"test:pawbar:{uuid.uuid4().hex[:8]}:"
    monkeypatch.setattr(admit_mod, "_KEY_PREFIX", name)
    yield name
    keys = [k async for k in redis.scan_iter(match=f"{name}*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(admit_mod, "_redis_skip_until", 0.0)


def _event(ref: str = "r-00000001", widget_id: str = "w1") -> PawBarEvent:
    return PawBarEvent(widget_id=widget_id, type="concierge_message", customer_ref=ref)


def _widget(overall: int = 60, per_customer: int = 10) -> Any:
    # admit reads only the two limits off the widget.
    return SimpleNamespace(rate_limit_per_min=overall, per_customer_limit_per_min=per_customer)


async def _admit(redis, ev, overall=5, per_customer=5, bucket="", now_ms=1_000_000):
    return await admit_mod.redis_admit(
        redis,
        ev,
        overall_per_min=overall,
        per_customer_per_min=per_customer,
        bucket=bucket,
        now_ms=now_ms,
    )


# --- Lua window (real Redis) -------------------------------------------------


async def test_overall_cap_admits_up_to_the_cap(redis, prefix):
    results = [await _admit(redis, _event(f"r-{i:08d}"), overall=3) for i in range(5)]
    assert results == [True, True, True, False, False]


async def test_per_customer_cap_is_separate_from_overall(redis, prefix):
    one = [await _admit(redis, _event("r-00000001"), overall=10, per_customer=2) for _ in range(3)]
    assert one == [True, True, False]
    assert await _admit(redis, _event("r-00000002"), overall=10, per_customer=2)


async def test_refused_event_is_not_counted(redis, prefix):
    for _ in range(3):
        await _admit(redis, _event("r-00000001"), overall=3, per_customer=1)
    # Only the first was admitted, so two overall slots remain for others.
    assert await _admit(redis, _event("r-00000002"), overall=3, per_customer=1)
    assert await _admit(redis, _event("r-00000003"), overall=3, per_customer=1)
    assert not await _admit(redis, _event("r-00000004"), overall=3, per_customer=1)


async def test_window_slides(redis, prefix):
    assert await _admit(redis, _event(), overall=1, now_ms=1_000_000)
    assert not await _admit(redis, _event(), overall=1, now_ms=1_059_999)
    assert await _admit(redis, _event(), overall=1, now_ms=1_060_001)


async def test_buckets_are_independent(redis, prefix):
    assert await _admit(redis, _event(), overall=1, bucket="events")
    assert not await _admit(redis, _event(), overall=1, bucket="events")
    assert await _admit(redis, _event(), overall=1, bucket="")


async def test_concurrent_admits_from_two_clients_never_exceed_the_cap(redis, prefix):
    other = aioredis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        clients = [redis, other]
        results = await asyncio.gather(
            *(
                _admit(clients[i % 2], _event(f"r-{i:08d}"), overall=7, per_customer=7)
                for i in range(40)
            )
        )
    finally:
        await other.aclose()
    assert sum(results) == 7


async def test_admitted_event_is_one_row_and_refused_is_none(
    redis, prefix, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(admit_mod, "_redis_or_none", lambda: redis)
    store = PawBarStore(tmp_path / "pb.db")
    widget = _widget(overall=2, per_customer=2)
    results = [
        await admit_mod.admit(store, _event(f"r-{i:08d}"), widget, bucket="events")
        for i in range(4)
    ]
    assert results == [True, True, False, False]
    since = datetime.fromtimestamp(0)
    assert await store.count_events_since("w1", since, bucket="events") == 2
    assert await store.count_events_since("w1", since) == 0


# --- routing (no Redis needed) -----------------------------------------------


class _Store:
    def __init__(self, admit_result: bool = True) -> None:
        self.admit_calls: list[dict] = []
        self.recorded: list[tuple[PawBarEvent, str]] = []
        self._admit_result = admit_result

    async def admit_event(self, event, **kw):
        self.admit_calls.append(kw)
        return self._admit_result

    async def record_event(self, event, *, bucket=""):
        self.recorded.append((event, bucket))
        return event


class _Redis:
    def __init__(self, result: int | Exception) -> None:
        self.result = result
        self.calls = 0

    async def eval(self, *args):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _use_redis(monkeypatch, fake) -> None:
    from pocketpaw_ee.cloud._core import redis_client

    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://example:6379/0")
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)


async def test_without_redis_url_the_store_admits(monkeypatch):
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    store = _Store()
    assert await admit_mod.admit(store, _event(), _widget(60, 10), bucket="events")
    assert store.admit_calls == [
        {"overall_per_min": 60, "per_customer_per_min": 10, "bucket": "events"}
    ]
    assert store.recorded == []


async def test_redis_admit_records_a_plain_insert(monkeypatch):
    fake = _Redis(1)
    _use_redis(monkeypatch, fake)
    store = _Store()
    ev = _event()
    assert await admit_mod.admit(store, ev, _widget(), bucket="events")
    assert store.recorded == [(ev, "events")]
    assert store.admit_calls == []


async def test_redis_refusal_writes_nothing(monkeypatch):
    _use_redis(monkeypatch, _Redis(0))
    store = _Store()
    assert not await admit_mod.admit(store, _event(), _widget())
    assert store.recorded == []
    assert store.admit_calls == []


async def test_redis_error_falls_back_to_the_store_and_backs_off(monkeypatch):
    fake = _Redis(ConnectionError("down"))
    _use_redis(monkeypatch, fake)
    store = _Store(admit_result=False)
    assert not await admit_mod.admit(store, _event(), _widget())
    assert len(store.admit_calls) == 1
    # Second call inside the back-off window never touches Redis.
    await admit_mod.admit(store, _event(), _widget())
    assert fake.calls == 1
    assert len(store.admit_calls) == 2


async def test_store_error_after_redis_admit_propagates(monkeypatch):
    import sqlite3

    _use_redis(monkeypatch, _Redis(1))

    class _Locked(_Store):
        async def record_event(self, event, *, bucket=""):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        await admit_mod.admit(_Locked(), _event(), _widget())
