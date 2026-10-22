# tests/ee/sites/test_preview_infra_retry.py — a preview / html-verify render whose last
# job failed for an INFRASTRUCTURE reason is queued again on the next request, instead of
# replaying that failure for the hour arq keeps the result.
#
# The preview and html-verify job ids are deterministic over (pocket, content hash), so a
# refused enqueue means "a job with this id exists or left a result". When that result is
# ``sandbox_unavailable:*`` (org capacity full, no sandbox, the job raised before reaching
# one), nothing about the site was wrong: the stored failure is freed and a new job queued.
# A failure of the site's own code (build_failed, scaffold_failed, …) stays sticky, since
# rebuilding identical inputs would only fail again. A poll while the new job is in
# flight attaches to it rather than opening another.
#
# The pool and ``Job`` stand-ins model the one arq behaviour that matters here: an id is
# refused while it has a live job OR a stored result, and deleting the result key frees it.
from __future__ import annotations

from typing import Any

import pytest
from arq.jobs import JobStatus
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import capacity


class _Info:
    def __init__(self, *, success: bool, result: Any) -> None:
        self.success = success
        self.result = result


class _ArqLike:
    """Job ids with a live job (``live``: id → JobStatus) or a stored result."""

    def __init__(self) -> None:
        self.live: dict[str, JobStatus] = {}
        self.results: dict[str, _Info] = {}
        self.enqueued: list[str] = []
        self.deleted: list[str] = []

    async def enqueue_job(self, _fn: str, *_a: Any, _job_id: str, **_k: Any) -> Any:
        if _job_id in self.live or _job_id in self.results:
            return None
        # A fresh preview job on a full org parks itself deferred, waiting for a slot.
        self.live[_job_id] = JobStatus.deferred
        self.enqueued.append(_job_id)
        return object()

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        job_id = key.removeprefix(bj.result_key_prefix)
        return 1 if self.results.pop(job_id, None) is not None else 0

    def finish(self, job_id: str, info: _Info) -> None:
        self.live.pop(job_id, None)
        self.results[job_id] = info


@pytest.fixture
def arq_like(monkeypatch) -> _ArqLike:
    pool = _ArqLike()

    class _Job:
        def __init__(self, job_id: str, _pool: Any, _queue_name: str | None = None) -> None:
            self.job_id = job_id

        async def status(self) -> JobStatus:
            if self.job_id in pool.live:
                return pool.live[self.job_id]
            return JobStatus.complete if self.job_id in pool.results else JobStatus.not_found

        async def result_info(self) -> Any:
            return pool.results.get(self.job_id)

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(bj, "Job", _Job)
    monkeypatch.setattr(bj, "_supersede_previous_job", _noop)
    return pool


def _failed(reason: str) -> _Info:
    return _Info(success=True, result={"status": "failed", "reason": reason})


INFRA_FAILURES = {
    "capacity": _failed(f"{bj.RUNG_SANDBOX_UNAVAILABLE}:{capacity.CAPACITY_CAUSE}"),
    "no_sandbox": _failed(f"{bj.RUNG_SANDBOX_UNAVAILABLE}:{capacity.NO_SANDBOX_CAUSE}"),
    # ``run_site_preview_build`` re-raises for a sandbox it never reached; the outcome
    # read maps that to ``sandbox_unavailable:job_raised``.
    "job_raised": _Info(success=False, result=RuntimeError("could not create sandbox")),
}

SITE_FAILURES = {
    "build_failed": _failed("build_failed:build_failed"),
    "install_failed": _failed("build_failed:install_failed"),
    "scaffold_failed": _failed(f"{bj.RUNG_SCAFFOLD_FAILED}:dependency_policy"),
}


async def _preview(pool: _ArqLike) -> bj.PreviewBuildEnqueue:
    return await bj.enqueue_preview_build(
        pocket_id="p1",
        content_hash="h1",
        engine="react",
        generator_input={},
        _pool_override=pool,
    )


async def _html_verify(pool: _ArqLike) -> bj.PreviewBuildEnqueue:
    return await bj.enqueue_html_verify(
        pocket_id="p1", content_hash="h1", generator_input={}, _pool_override=pool
    )


LANES = {
    "preview": (_preview, bj._preview_job_id("p1", "h1")),
    "html_verify": (_html_verify, bj.html_verify_job_id("p1", "h1")),
}


@pytest.mark.parametrize("lane", list(LANES))
@pytest.mark.parametrize("failure", list(INFRA_FAILURES))
async def test_an_infra_failure_is_queued_again_not_replayed(
    arq_like: _ArqLike, lane: str, failure: str
) -> None:
    enqueue, job_id = LANES[lane]
    arq_like.finish(job_id, INFRA_FAILURES[failure])

    out = await enqueue(arq_like)

    assert out.status == "queued", f"{failure} replayed as {out.status} / {out.reason}"
    assert out.reason is None
    assert arq_like.enqueued == [job_id], "exactly one new job for this render"
    assert arq_like.deleted == [bj.result_key_prefix + job_id]


@pytest.mark.parametrize("lane", list(LANES))
async def test_a_poll_while_the_retry_waits_attaches_to_it(arq_like: _ArqLike, lane: str) -> None:
    """The retried job goes through the capacity budget itself. Every poll meanwhile
    reads the one job (queued / waiting_for_capacity on the preview lane), never a new
    one, so a persistently full org costs one job per id at a time."""
    enqueue, job_id = LANES[lane]
    arq_like.finish(job_id, INFRA_FAILURES["capacity"])
    await enqueue(arq_like)

    polls = [await enqueue(arq_like) for _ in range(3)]

    assert arq_like.enqueued == [job_id]
    if lane == "preview":
        assert {(p.status, p.reason) for p in polls} == {("queued", capacity.WAITING_REASON)}
    else:
        assert {p.status for p in polls} == {"building"}


@pytest.mark.parametrize("failure", list(SITE_FAILURES))
async def test_a_failure_of_the_sites_own_code_stays_sticky(
    arq_like: _ArqLike, failure: str
) -> None:
    """The rebuild-loop guard: identical inputs that failed on their own merits are
    reported failed and NOT rebuilt per poll."""
    job_id = bj._preview_job_id("p1", "h1")
    info = SITE_FAILURES[failure]
    arq_like.finish(job_id, info)

    out = await _preview(arq_like)

    assert out.status == "failed"
    assert out.reason == info.result["reason"]
    assert arq_like.enqueued == []
    assert arq_like.deleted == []
    assert job_id in arq_like.results


@pytest.mark.parametrize("failure", list(SITE_FAILURES))
async def test_html_verify_keeps_a_site_failure_too(arq_like: _ArqLike, failure: str) -> None:
    job_id = bj.html_verify_job_id("p1", "h1")
    arq_like.finish(job_id, SITE_FAILURES[failure])

    await _html_verify(arq_like)

    assert arq_like.enqueued == []
    assert arq_like.deleted == []
