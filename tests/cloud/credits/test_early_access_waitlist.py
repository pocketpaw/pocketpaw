# tests/cloud/credits/test_early_access_waitlist.py: the public early-access
# waitlist, POST /api/v1/early-access/waitlist.
#
# Real route, real rate limiter, real in-memory Mongo with the unique index. A
# join stores one lowercased row; a repeat join answers the same 204 and adds
# nothing (no enumeration); the sixth join in an hour from one IP is refused with
# 429 ``early_access.waitlist_rate_limited``; a malformed email is a 422.
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_limiter():
    """The limiter is per process and keyed on the client IP every test shares."""
    from pocketpaw_ee.cloud._core import rate_limit

    rate_limit._early_access_waitlist_limiter._buckets.clear()
    yield
    rate_limit._early_access_waitlist_limiter._buckets.clear()


def _client() -> AsyncClient:
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.early_access.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


URL = "/api/v1/early-access/waitlist"


async def test_join_stores_one_lowercased_row(mongo_db):
    from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry

    async with _client() as client:
        resp = await client.post(URL, json={"email": "Ada@Example.COM", "source": "ripple"})

    assert resp.status_code == 204
    rows = await WaitlistEntry.find_all().to_list()
    assert [(r.email, r.source) for r in rows] == [("ada@example.com", "ripple")]
    assert rows[0].created_at is not None


async def test_repeat_join_is_deduped_and_answers_the_same(mongo_db):
    from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry

    async with _client() as client:
        first = await client.post(URL, json={"email": "ada@example.com"})
        second = await client.post(URL, json={"email": " ADA@example.com "})

    assert first.status_code == second.status_code == 204
    assert await WaitlistEntry.find_all().count() == 1


async def test_sixth_join_in_an_hour_from_one_ip_is_rate_limited(mongo_db):
    from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry

    async with _client() as client:
        for i in range(5):
            ok = await client.post(URL, json={"email": f"u{i}@example.com"})
            assert ok.status_code == 204
        refused = await client.post(URL, json={"email": "u5@example.com"})

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "early_access.waitlist_rate_limited"
    assert await WaitlistEntry.find_all().count() == 5


async def test_malformed_email_is_rejected(mongo_db):
    from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry

    async with _client() as client:
        resp = await client.post(URL, json={"email": "not-an-email"})

    assert resp.status_code == 422
    assert await WaitlistEntry.find_all().count() == 0
