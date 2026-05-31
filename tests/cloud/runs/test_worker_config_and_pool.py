"""Review findings #4, #10 + the descriptor regression — worker Redis-settings
must be a concrete ``RedisSettings`` instance in ``WorkerSettings.__dict__``,
and the arq pool must close on web shutdown.

Why eager evaluation (not a descriptor): arq's ``get_kwargs`` reads
``settings_cls.__dict__`` directly (arq/worker.py:889), which bypasses the
descriptor protocol. A previous attempt to use a non-data descriptor here
shipped to staging and crashed on boot with
``AttributeError: '_LazyRedisSettings' object has no attribute 'host'``
when arq passed the descriptor instance through to ``create_pool``. This
test pins the contract: a real RedisSettings, in ``__dict__``, no
descriptor magic.

#4: ``WorkerSettings.redis_settings`` used to silently default to
    ``redis://localhost:6379/0`` if ``POCKETPAW_REDIS_URL`` was unset,
    while ``ArqExecutor._get_pool`` raised loudly. A typoed env in prod
    would split-brain (web → prod-Redis, worker → localhost).

#10: ``arq_executor._pool`` had no aclose hook — a web process that ever
     enqueued a job leaked the connection through shutdown.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from arq.connections import RedisSettings
from pocketpaw_ee.cloud.chat.runs import arq_executor
from pocketpaw_ee.cloud.chat.runs import worker as worker_mod

pytestmark = pytest.mark.asyncio


# --- arq-compat contract: real RedisSettings in __dict__ -------------------


async def test_worker_settings_redis_settings_is_a_real_redis_settings():
    """Regression for the _LazyRedisSettings deploy crash.

    arq reads ``settings_cls.__dict__['redis_settings']`` directly, so a
    descriptor here gets handed to ``create_pool`` and crashes with
    ``AttributeError: '<descriptor>' object has no attribute 'host'``.
    The value in ``__dict__`` MUST be a concrete ``RedisSettings``.
    """
    raw = worker_mod.WorkerSettings.__dict__.get("redis_settings")
    assert isinstance(raw, RedisSettings), (
        f"WorkerSettings.redis_settings in __dict__ is {type(raw).__name__}, "
        "expected RedisSettings. Eager evaluation is required because arq "
        "bypasses the descriptor protocol via __dict__ access."
    )
    # Sanity: arq will read these attributes during create_pool.
    assert isinstance(raw.host, str) and raw.host
    assert isinstance(raw.port, int)


# --- #4: helper fails loud when env is missing -----------------------------


async def test_redis_settings_helper_raises_when_env_unset(monkeypatch):
    """``_redis_settings()`` must refuse to default to localhost — that was
    review finding #4. The module import already ran successfully (env is
    set via conftest); we test the helper in isolation."""
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
        worker_mod._redis_settings()


async def test_redis_settings_helper_parses_env(monkeypatch):
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://parsed-host:6380/2")
    settings = worker_mod._redis_settings()
    assert settings.host == "parsed-host"
    assert settings.port == 6380
    assert settings.database == 2


# --- #10: arq pool close on shutdown ---------------------------------------


async def test_close_pool_aclose_called_when_pool_exists(monkeypatch):
    """close_pool must actually invoke aclose() on the cached pool, then
    null the reference so subsequent _get_pool builds a fresh one."""
    arq_executor._reset_for_tests()

    fake_pool = AsyncMock()
    fake_pool.aclose = AsyncMock()
    monkeypatch.setattr(arq_executor, "_pool", fake_pool)

    await arq_executor.close_pool()

    fake_pool.aclose.assert_awaited_once()
    assert arq_executor._pool is None


async def test_close_pool_is_safe_when_no_pool_exists():
    """Web processes that never enqueued a Tier 2 job will call close_pool
    on shutdown — it must be a no-op, not raise AttributeError."""
    arq_executor._reset_for_tests()

    # Should not raise.
    await arq_executor.close_pool()
    assert arq_executor._pool is None


async def test_close_pool_swallows_aclose_failure(monkeypatch, caplog):
    """A failing aclose() during shutdown must not propagate — shutdown
    paths can't afford to raise."""
    import logging

    arq_executor._reset_for_tests()

    fake_pool = AsyncMock()
    fake_pool.aclose = AsyncMock(side_effect=RuntimeError("redis lost"))
    monkeypatch.setattr(arq_executor, "_pool", fake_pool)

    with caplog.at_level(logging.DEBUG, logger="pocketpaw_ee.cloud.chat.runs.arq_executor"):
        await arq_executor.close_pool()  # must not raise

    assert arq_executor._pool is None  # ref cleared even on failure
