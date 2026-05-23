"""Review findings #4, #7, #10 — worker Redis-settings consistency + arq
pool shutdown.

#4: ``WorkerSettings.redis_settings`` used to silently default to
    ``redis://localhost:6379/0`` if ``POCKETPAW_REDIS_URL`` was unset,
    while ``ArqExecutor._get_pool`` raised loudly. A typoed env in prod
    would split-brain (web → prod-Redis, worker → localhost).

#7: ``WorkerSettings.redis_settings`` was read at module import time, so
    a test that ``monkeypatch.setenv`` after import had no effect.

#10: ``arq_executor._pool`` had no aclose hook — a web process that ever
     enqueued a job leaked the connection through shutdown.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.chat.runs import arq_executor
from pocketpaw_ee.cloud.chat.runs import worker as worker_mod

pytestmark = pytest.mark.asyncio


# --- #4 + #7: redis_settings is lazy + fail-loud ---------------------------


async def test_worker_redis_settings_raises_when_env_unset(monkeypatch):
    """Accessing WorkerSettings.redis_settings without POCKETPAW_REDIS_URL
    set must raise — silent fallback to localhost is a deploy footgun."""
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)

    with pytest.raises(RuntimeError, match="POCKETPAW_REDIS_URL"):
        worker_mod.WorkerSettings.redis_settings  # noqa: B018  (descriptor access)


async def test_worker_redis_settings_lazy_reads_env_after_import(monkeypatch):
    """Regression for #7 — env is read at access time, so a test (or a
    deployment whose env loader runs after import) can set the var and the
    next access picks it up."""
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://late-loaded:6379/3")

    settings = worker_mod.WorkerSettings.redis_settings

    # arq's RedisSettings exposes host/port from from_dsn.
    assert settings.host == "late-loaded"
    assert settings.port == 6379
    assert settings.database == 3


async def test_worker_redis_settings_reflects_changed_env(monkeypatch):
    """Subsequent access reflects the current env — no cached/stale value."""
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://first:6379/0")
    s1 = worker_mod.WorkerSettings.redis_settings
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://second:6379/0")
    s2 = worker_mod.WorkerSettings.redis_settings

    assert s1.host == "first"
    assert s2.host == "second"


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
