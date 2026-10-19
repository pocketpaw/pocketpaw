# tests/ee/sites/test_preview_retry_infra_failure.py — which refused preview /
# html-verify enqueues free their job id and queue the render again.
#
# The preview lane's job id is deterministic over (pocket, content hash), and arq keeps a
# finished job's result for an hour, refusing the id until then. A render that failed for
# an INFRASTRUCTURE reason (no generator on the host, no sandbox, a lost container) must be
# queued again once the cause is fixed, not read back as "failed" for that hour. A render
# that failed because of its own source (scaffold, compile) must not: rebuilding identical
# inputs fails identically. An aborted (superseded) render is re-queued at once, as before.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pocketpaw_ee.sites import build_job as bj

_JOB_ID = bj._preview_job_id("p1", "h1")
_LONG_AGO = datetime.now(UTC) - timedelta(minutes=10)


class _Info:
    def __init__(self, *, success: bool, result: Any, finished: datetime = _LONG_AGO) -> None:
        self.success = success
        self.result = result
        self.finish_time = finished


class _Pool:
    """Refuses the first enqueue (the id is held by a kept result), accepts after that."""

    def __init__(self) -> None:
        self.enqueues = 0
        self.deleted: list[str] = []
        self.value: str | None = None

    async def enqueue_job(self, *_a: Any, **_k: Any) -> Any:
        self.enqueues += 1
        return None if self.enqueues == 1 else object()

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1

    async def getset(self, key: str, value: str) -> Any:
        previous, self.value = self.value, value
        return previous.encode() if previous else None

    async def expire(self, key: str, seconds: int) -> bool:
        return True


@pytest.fixture
def stored(monkeypatch):
    """Set what arq holds for the job id: ``stored(info)``."""
    holder: dict[str, Any] = {}

    async def _result_info(self):  # noqa: ANN001
        return holder.get("info")

    async def _outcome(pool: Any, job_id: str):
        info = holder.get("info")
        if info is not None and isinstance(info.result, dict):
            return info.result.get("status"), info.result.get("reason")
        return "building", None

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(bj.Job, "result_info", _result_info)
    monkeypatch.setattr(bj, "_preview_job_outcome", _outcome)
    monkeypatch.setattr(bj, "_supersede_previous_job", _noop)

    def _set(info: _Info | None) -> None:
        holder["info"] = info

    return _set


async def _enqueue(pool: _Pool) -> bj.PreviewBuildEnqueue:
    return await bj.enqueue_preview_build(
        pocket_id="p1",
        content_hash="h1",
        engine="project",
        generator_input={},
        _pool_override=pool,
    )


def _failed(reason: str) -> _Info:
    return _Info(success=True, result={"status": "failed", "reason": reason})


class TestInfraFailureIsRetried:
    @pytest.mark.parametrize(
        "reason",
        [
            f"{bj.RUNG_SANDBOX_UNAVAILABLE}:generator_missing",
            f"{bj.RUNG_SANDBOX_UNAVAILABLE}:no_sandbox",
            f"{bj.RUNG_SANDBOX_UNAVAILABLE}:capacity",
            "infra_lost:no_sentinel_before_timeout",
            "timed_out:no_sentinel_at_timeout",
            f"{bj.RUNG_ARTIFACT_MISSING}:download_delivered_no_bytes",
        ],
    )
    async def test_an_infra_failed_render_is_queued_again(self, stored, reason: str) -> None:
        stored(_failed(reason))
        pool = _Pool()

        out = await _enqueue(pool)

        assert out.status == "queued"
        assert pool.enqueues == 2
        assert pool.deleted == [bj.result_key_prefix + _JOB_ID]

    async def test_a_job_that_raised_is_queued_again(self, stored) -> None:
        # The job re-raises only when no sandbox could be created.
        stored(_Info(success=False, result=RuntimeError("daytona down")))
        pool = _Pool()

        assert (await _enqueue(pool)).status == "queued"
        assert pool.enqueues == 2

    async def test_a_fresh_infra_failure_is_reported_not_rebuilt_on_every_poll(
        self, stored
    ) -> None:
        # A client polling every 2s must see the failure, not a rebuild loop.
        stored(
            _Info(
                success=True,
                result={"status": "failed", "reason": "sandbox_unavailable:no_sandbox"},
                finished=datetime.now(UTC),
            )
        )
        pool = _Pool()

        out = await _enqueue(pool)

        assert (out.status, out.reason) == ("failed", "sandbox_unavailable:no_sandbox")
        assert pool.enqueues == 1
        assert pool.deleted == []


class TestOtherResults:
    async def test_an_aborted_render_is_still_queued_again_at_once(self, stored) -> None:
        stored(_Info(success=False, result=asyncio.CancelledError(), finished=datetime.now(UTC)))
        pool = _Pool()

        assert (await _enqueue(pool)).status == "queued"
        assert pool.deleted == [bj.result_key_prefix + _JOB_ID]

    async def test_a_superseded_render_is_queued_again_at_once(self, stored) -> None:
        stored(
            _Info(
                success=True,
                result={"status": "failed", "reason": bj.SUPERSEDED_REASON},
                finished=datetime.now(UTC),
            )
        )
        pool = _Pool()

        assert (await _enqueue(pool)).status == "queued"

    @pytest.mark.parametrize(
        "reason",
        [
            f"{bj.RUNG_SCAFFOLD_FAILED}:bad_tree",
            f"{bj.RUNG_SCAFFOLD_EMPTY}:no_files_generated",
            "build_failed:nonzero_exit",
            f"{bj.RUNG_ENGINE_NOT_BUILDABLE}:ripple",
            f"{bj.RUNG_PREVIEW_UNREADABLE}:bundle_unreadable",
            None,
        ],
    )
    async def test_a_source_caused_failure_keeps_single_flight(
        self, stored, reason: str | None
    ) -> None:
        stored(_failed(reason) if reason else _Info(success=True, result="not a dict"))
        pool = _Pool()

        out = await _enqueue(pool)

        assert out.status != "queued"
        assert pool.enqueues == 1
        assert pool.deleted == []

    async def test_a_built_render_is_not_cleared(self, stored) -> None:
        stored(_Info(success=True, result={"status": "built", "reason": "completed_ok:ok"}))
        pool = _Pool()

        await _enqueue(pool)

        assert pool.enqueues == 1


class TestHelper:
    async def test_never_raises_on_a_broken_pool(self, monkeypatch) -> None:
        async def _boom(self):  # noqa: ANN001
            raise ConnectionError("redis gone")

        monkeypatch.setattr(bj.Job, "result_info", _boom)
        assert await bj._clear_retryable_result(object(), _JOB_ID) is False

    async def test_never_raises_when_the_delete_fails(self, stored) -> None:
        stored(_failed("sandbox_unavailable:generator_missing"))

        class _BadDelete:
            async def delete(self, key: str) -> int:
                raise ConnectionError("redis gone")

        assert await bj._clear_retryable_result(_BadDelete(), _JOB_ID) is False

    async def test_no_result_is_nothing_to_clear(self, stored) -> None:
        stored(None)
        assert await bj._clear_retryable_result(_Pool(), _JOB_ID) is False


class TestHtmlVerifyLane:
    async def test_an_infra_failed_html_verify_is_queued_again(self, stored) -> None:
        stored(_failed(f"{bj.RUNG_SANDBOX_UNAVAILABLE}:no_sandbox"))
        pool = _Pool()

        out = await bj.enqueue_html_verify(
            pocket_id="p1", content_hash="h1", generator_input={}, _pool_override=pool
        )

        assert out.status == "queued"
        assert pool.deleted == [bj.result_key_prefix + bj.html_verify_job_id("p1", "h1")]
