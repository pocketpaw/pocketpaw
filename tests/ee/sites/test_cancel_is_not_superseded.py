# tests/ee/sites/test_cancel_is_not_superseded.py — only a real supersede reads
# "superseded".
#
# A preview / html-verify job whose stored result is a ``CancelledError`` used to read
# ``superseded`` whatever cancelled it. But arq also stores one for a job aborted before
# start by a STALE abort mark (an abort requested for a deterministic job id that later
# came back as the pocket's current render), and a shutdown or deploy cancel is not a
# newer edit either. Telling the owner "a newer edit replaced this build" when nothing
# did hides a lost build. Now ``_supersede_previous_job`` records the job it aborts, and
# a cancel reads ``superseded`` only when that record exists AND the job is no longer
# the pocket's current one. Any other cancel reads ``job_crashed:cancelled`` and spends
# the budgeted ``raised`` retry, like any other job that died under us.
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from arq.jobs import JobStatus
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import capacity

pytestmark = pytest.mark.asyncio


class _Info:
    def __init__(self, *, success: bool, result: Any) -> None:
        self.success = success
        self.result = result


class _Redis:
    """A dict standing in for ArqRedis: the pointer, the supersede record, counters."""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.results: dict[str, _Info] = {}

    async def getset(self, key: str, value: str) -> Any:
        previous, self.kv[key] = self.kv.get(key), value
        return previous.encode() if isinstance(previous, str) else previous

    async def get(self, key: str) -> Any:
        value = self.kv.get(key)
        return value.encode() if isinstance(value, str) else value

    async def set(self, key: str, value: Any, *, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def incr(self, key: str) -> int:
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def delete(self, key: str) -> int:
        job_id = key.removeprefix(bj.result_key_prefix)
        return 1 if self.results.pop(job_id, None) is not None else 0


@pytest.fixture
def redis(monkeypatch) -> _Redis:
    pool = _Redis()

    class _Job:
        def __init__(self, job_id: str, _pool: Any, _queue_name: str | None = None) -> None:
            self.job_id = job_id

        async def status(self) -> JobStatus:
            return JobStatus.complete

        async def result_info(self) -> Any:
            return pool.results.get(self.job_id)

        async def abort(self, *, timeout: float | None = None) -> bool:
            raise TimeoutError  # requested; nobody waits for the worker

        async def result(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> Any:
            info = pool.results[self.job_id]
            raise info.result

    monkeypatch.setattr(bj, "Job", _Job)
    monkeypatch.setattr(bj, "_LOGGED_RAISES", {})
    return pool


OLD = bj._preview_job_id("p1", "old")
NEW = bj._preview_job_id("p1", "new")


def _cancelled() -> _Info:
    return _Info(success=False, result=asyncio.CancelledError())


async def test_supersede_records_the_job_it_aborted(redis: _Redis) -> None:
    await bj._supersede_previous_job(redis, "p1", OLD)
    assert await bj._supersede_previous_job(redis, "p1", NEW) == OLD

    assert await bj.was_superseded(redis, OLD) is True
    assert await bj.was_superseded(redis, NEW) is False


async def test_a_superseded_job_reads_superseded(redis: _Redis) -> None:
    await bj._supersede_previous_job(redis, "p1", OLD)
    await bj._supersede_previous_job(redis, "p1", NEW)
    redis.results[OLD] = _cancelled()

    assert await bj._preview_job_outcome(redis, OLD) == ("failed", bj.SUPERSEDED_REASON)


async def test_a_shutdown_cancel_reads_as_a_crash_not_superseded(redis: _Redis) -> None:
    """Nothing replaced this job: the worker went away under it."""
    await bj._supersede_previous_job(redis, "p1", NEW)
    redis.results[NEW] = _cancelled()

    status, reason = await bj._preview_job_outcome(redis, NEW)

    assert (status, reason) == ("failed", bj.JOB_CANCELLED_REASON)
    assert reason.split(":", 1)[0] == bj.RUNG_JOB_CRASHED
    assert capacity.reason_message(reason) == capacity.JOB_CRASHED_MESSAGE


async def test_a_job_that_came_back_as_current_is_not_superseded(redis: _Redis) -> None:
    """A stale abort mark: OLD was superseded, then an undo made OLD current again and
    arq aborted the new run before start. The pocket's current job is OLD, so this is
    not a newer edit."""
    await bj._supersede_previous_job(redis, "p1", OLD)
    await bj._supersede_previous_job(redis, "p1", NEW)
    await bj._supersede_previous_job(redis, "p1", OLD)
    redis.results[OLD] = _cancelled()

    assert await bj._preview_job_outcome(redis, OLD) == ("failed", bj.JOB_CANCELLED_REASON)


async def test_a_superseded_job_is_retried_unbudgeted_a_cancelled_one_budgeted(
    redis: _Redis,
) -> None:
    await bj._supersede_previous_job(redis, "p1", OLD)
    await bj._supersede_previous_job(redis, "p1", NEW)
    redis.results[OLD] = _cancelled()
    redis.results[NEW] = _cancelled()

    assert bj._retry_kind(_cancelled(), superseded=True) == "aborted"
    assert bj._retry_kind(_cancelled()) == "raised"

    assert await bj._clear_retryable_result(redis, OLD) is True
    assert not any(k.startswith(bj._RETRY_COUNTER_PREFIX) and OLD in k for k in redis.kv)
    assert await bj._clear_retryable_result(redis, NEW) is True
    assert any(k.startswith(bj._RETRY_COUNTER_PREFIX) and NEW in k for k in redis.kv)


async def test_a_waiter_reads_superseded_only_for_a_real_supersede(redis: _Redis) -> None:
    await bj._supersede_previous_job(redis, "p1", OLD)
    await bj._supersede_previous_job(redis, "p1", NEW)
    redis.results[OLD] = _cancelled()
    redis.results[NEW] = _cancelled()

    replaced = await bj.wait_for_preview_result(redis, OLD, timeout=1)
    lost = await bj.wait_for_preview_result(redis, NEW, timeout=1)

    assert replaced["reason"] == bj.SUPERSEDED_REASON
    assert lost["status"] == "failed"
    assert lost["reason"] == bj.JOB_CANCELLED_REASON


async def test_an_unreadable_record_reads_as_not_superseded() -> None:
    assert await bj.was_superseded(object(), OLD) is False


async def test_the_record_is_written_before_the_abort_is_requested(
    redis: _Redis, monkeypatch
) -> None:
    """The worker can cancel the old job inside the abort's wait, and a reader that
    looks then must already find the record."""
    seen: list[bool] = []

    original = bj.Job

    class _Watching(original):  # type: ignore[misc, valid-type]
        async def abort(self, *, timeout: float | None = None) -> bool:
            seen.append(await bj.was_superseded(redis, self.job_id))
            raise TimeoutError

    monkeypatch.setattr(bj, "Job", _Watching)
    await bj._supersede_previous_job(redis, "p1", OLD)
    await bj._supersede_previous_job(redis, "p1", NEW)

    assert seen == [True]
