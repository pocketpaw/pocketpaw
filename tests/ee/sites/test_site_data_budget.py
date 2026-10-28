# tests/ee/sites/test_site_data_budget.py — the Cloudflare call limiter the Sites Data
# dashboard spends before every D1 / R2 / KV request (``sites.site_data.budget``).
#
# Pinned: the global, R2 and per-workspace buckets each refuse once spent; one
# workspace spending its bucket does not block another; a refused call spends nothing
# from the other buckets; the refusal is a 429 ``sites.data_busy`` carrying
# ``retry_after``; the Redis path (fakeredis with Lua) shares the same rules; a broken
# Redis falls back to the in-process buckets instead of failing the read; and the
# ``Budgeted`` wrapper charges one token per Cloudflare call, not per statement.
from __future__ import annotations

from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pocketpaw_ee.sites.site_data import budget


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    budget.reset()
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    yield
    budget.reset()


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(budget, "GLOBAL_LIMIT", (5, 300))
    monkeypatch.setattr(budget, "R2_LIMIT", (2, 300))
    monkeypatch.setattr(budget, "WORKSPACE_LIMIT", (3, 60))


@pytest.mark.asyncio
async def test_workspace_bucket_refuses_with_retry_after(small):
    for _ in range(3):
        await budget.acquire("d1", "ws1")
    with pytest.raises(budget.DataBusy) as exc:
        await budget.acquire("d1", "ws1")
    err = exc.value
    assert err.status_code == 429 and err.code == "sites.data_busy"
    assert 1 <= err.details["retry_after"] <= 60
    assert err.to_dict()["error"]["details"]["retry_after"] == err.details["retry_after"]


@pytest.mark.asyncio
async def test_one_workspace_does_not_block_another_until_global_runs_out(small):
    for _ in range(3):
        await budget.acquire("d1", "ws1")
    await budget.acquire("d1", "ws2")
    await budget.acquire("d1", "ws2")
    # global is 5: ws1 spent 3, ws2 spent 2
    with pytest.raises(budget.DataBusy):
        await budget.acquire("d1", "ws3")


@pytest.mark.asyncio
async def test_r2_bucket_is_separate(small):
    await budget.acquire("r2", "ws1")
    await budget.acquire("r2", "ws2")
    with pytest.raises(budget.DataBusy):
        await budget.acquire("r2", "ws3")
    await budget.acquire("d1", "ws3")  # D1 still has global budget


@pytest.mark.asyncio
async def test_a_refused_call_spends_nothing(small):
    for _ in range(3):
        await budget.acquire("d1", "ws1")  # ws1 full, global at 3 of 5
    for _ in range(5):
        with pytest.raises(budget.DataBusy):
            await budget.acquire("d1", "ws1")
    # The refusals took nothing from global, so ws2 still gets its two calls.
    await budget.acquire("d1", "ws2")
    await budget.acquire("d1", "ws2")


@pytest.mark.asyncio
async def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        await budget.acquire("sql", "ws1")


@pytest.mark.asyncio
async def test_redis_path_shares_the_rules(small):
    redis = FakeAsyncRedis(decode_responses=True)
    budget.use_redis(redis)
    try:
        for _ in range(3):
            await budget.acquire("d1", "ws1")
        with pytest.raises(budget.DataBusy) as exc:
            await budget.acquire("d1", "ws1")
        assert exc.value.details["retry_after"] >= 1
        assert int(await redis.get("cf:data:ws:ws1")) == 3
        assert int(await redis.get("cf:data:global")) == 3
        assert await redis.ttl("cf:data:ws:ws1") > 0
    finally:
        await redis.aclose()


class _BrokenRedis:
    async def eval(self, *a: Any, **k: Any) -> Any:
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_broken_redis_falls_back_to_in_process(small):
    budget.use_redis(_BrokenRedis())
    for _ in range(3):
        await budget.acquire("d1", "ws1")
    with pytest.raises(budget.DataBusy):
        await budget.acquire("d1", "ws1")


class _CF:
    def __init__(self) -> None:
        self.calls = 0

    async def query_d1(self, *, database_id: str, sql: str, params=None):
        self.calls += 1
        return [{"n": 1}]

    async def query_d1_batch(self, *, database_id: str, statements):
        self.calls += 1
        return [[{"n": 1}] for _ in statements]


@pytest.mark.asyncio
async def test_budgeted_client_charges_one_token_per_call(small):
    cf = _CF()
    wrapped = budget.Budgeted(cf, "ws1")
    await wrapped.query_d1_batch(database_id="d", statements=[("SELECT 1", [])] * 20)
    await wrapped.query_d1(database_id="d", sql="SELECT 1")
    await wrapped.query_d1(database_id="d", sql="SELECT 1")
    with pytest.raises(budget.DataBusy):
        await wrapped.query_d1(database_id="d", sql="SELECT 1")
    assert cf.calls == 3
