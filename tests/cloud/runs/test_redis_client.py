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
