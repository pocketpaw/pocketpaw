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
# flight attaches to it rather than opening another. Each id may be re-queued only a
# bounded number of times per hour (a raised job twice, sandbox_unavailable three
# times), so a deterministic fault ends in ``failed`` instead of an endless rebuild.
#
# A job that RAISED is read for what it raised, not lumped in as a lost sandbox: arq's
# own timeout reads ``timed_out:job_timeout``, arq refusing a run past ``max_tries``
# (only capacity retries spend tries) reads ``sandbox_unavailable:capacity``, a sandbox
# the job never got reads ``sandbox_unavailable:no_sandbox``, and anything else is our
# crash, ``job_crashed:unhandled_exception``. Each read logs the exception class and a
# redacted message once per job, so the worker's failure is visible from the API side.
#
# The pool and ``Job`` stand-ins model the one arq behaviour that matters here: an id is
# refused while it has a live job OR a stored result, and deleting the result key frees it,
# plus INCR / EXPIRE for the per-id retry counter.
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from arq.jobs import JobStatus
from arq.worker import JobExecutionFailed
from pocketpaw_ee.sites import build_job as bj
from pocketpaw_ee.sites import capacity


class _Info:
    def __init__(self, *, success: bool, result: Any) -> None:
        self.success = success
        self.result = result


class _NoCounter:
    """Job ids with a live job (``live``: id → JobStatus) or a stored result. No INCR."""

    def __init__(self) -> None:
        self.live: dict[str, JobStatus] = {}
        self.results: dict[str, _Info] = {}
        self.enqueued: list[str] = []
        self.deleted: list[str] = []
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

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


class _ArqLike(_NoCounter):
    """The same, with the INCR / EXPIRE the per-id retry counter needs."""

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def lapse_counters(self) -> None:
        """Every counter's TTL ran out."""
        self.counters.clear()
        self.ttls.clear()


@pytest.fixture(autouse=True)
def _fresh_raise_log(monkeypatch) -> None:
    """The read logs a raise once per job result per process; these stand-ins carry no
    finish time, so every test starts with an empty memo."""
    monkeypatch.setattr(bj, "_LOGGED_RAISES", {})


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
    # Any job that raised is our failure, never the site's, so it is queued again.
    "job_raised": _Info(success=False, result=RuntimeError("could not create sandbox")),
    "arq_timeout": _Info(success=False, result=TimeoutError()),
    "max_tries": _Info(success=False, result=JobExecutionFailed("max 7 retries exceeded")),
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


def _raised(exc: BaseException) -> _Info:
    return _Info(success=False, result=exc)


BUDGETED = {
    # A deterministic raise (a code bug, a bad verify image, a Daytona 4xx).
    "deterministic_raise": (
        _raised(ValueError("bad image")),
        bj.RETRY_LIMITS["raised"],
        "job_crashed:unhandled_exception",
    ),
    # arq's own timeout: a build the site makes overrun.
    "arq_timeout": (_raised(TimeoutError()), bj.RETRY_LIMITS["raised"], "timed_out:job_timeout"),
    # arq refusing a run past ``max_tries``: only capacity retries spend tries, so this is
    # a capacity wait that ran out, budgeted like a settled capacity failure.
    "max_tries_after_capacity": (
        _raised(JobExecutionFailed("max 7 retries exceeded")),
        bj.RETRY_LIMITS["sandbox_unavailable"],
        "sandbox_unavailable:capacity",
    ),
    "capacity": (
        INFRA_FAILURES["capacity"],
        bj.RETRY_LIMITS["sandbox_unavailable"],
        "sandbox_unavailable:capacity",
    ),
    "no_sandbox": (
        INFRA_FAILURES["no_sandbox"],
        bj.RETRY_LIMITS["sandbox_unavailable"],
        "sandbox_unavailable:no_sandbox",
    ),
}


async def _fail_every_run(pool: _ArqLike, job_id: str, info: _Info, runs: int) -> list[Any]:
    """Each job this render gets ends with ``info``; return every enqueue's answer."""
    pool.finish(job_id, info)
    answers = []
    for _ in range(runs):
        answers.append(await _preview(pool))
        if job_id in pool.live:
            pool.finish(job_id, info)
    return answers


@pytest.mark.parametrize("case", list(BUDGETED))
async def test_a_failure_that_keeps_recurring_is_retried_a_bounded_number_of_times(
    arq_like: _ArqLike, case: str
) -> None:
    """Without the bound, every poll after the job ends would clear and re-enqueue: one
    job (often one sandbox) per job length, forever, and the client would never see
    ``failed``."""
    info, limit, reason = BUDGETED[case]
    job_id = bj._preview_job_id("p1", "h1")

    answers = await _fail_every_run(arq_like, job_id, info, runs=limit + 3)

    assert len(arq_like.enqueued) == limit
    assert [a.status for a in answers[:limit]] == ["queued"] * limit
    assert {a.status for a in answers[limit:]} == {"failed"}
    assert {a.reason for a in answers[limit:]} == {reason}
    key = bj._RETRY_COUNTER_PREFIX + job_id
    assert arq_like.ttls[key] == bj.RETRY_WINDOW_SECONDS


async def test_the_budget_comes_back_when_the_counter_expires(arq_like: _ArqLike) -> None:
    job_id = bj._preview_job_id("p1", "h1")
    info = INFRA_FAILURES["capacity"]
    limit = bj.RETRY_LIMITS["sandbox_unavailable"]
    await _fail_every_run(arq_like, job_id, info, runs=limit + 1)
    assert (await _preview(arq_like)).status == "failed"

    arq_like.lapse_counters()

    assert (await _preview(arq_like)).status == "queued"
    assert len(arq_like.enqueued) == limit + 1


@pytest.mark.parametrize("failure", ["job_raised", "capacity"])
async def test_without_a_counter_a_budgeted_failure_is_not_retried(
    monkeypatch, failure: str
) -> None:
    pool = _NoCounter()
    job_id = bj._preview_job_id("p1", "h1")
    pool.finish(job_id, INFRA_FAILURES[failure])

    class _Job:
        def __init__(self, job_id: str, _pool: Any, _queue_name: str | None = None) -> None:
            self.job_id = job_id

        async def status(self) -> JobStatus:
            return JobStatus.complete

        async def result_info(self) -> Any:
            return pool.results.get(self.job_id)

    monkeypatch.setattr(bj, "Job", _Job)

    out = await _preview(pool)

    assert out.status == "failed"
    assert pool.enqueued == []


async def test_an_aborted_render_is_not_budgeted(arq_like: _ArqLike) -> None:
    """An abort only happens when another job took the pocket, so undoing back to this
    render keeps working however often it happens."""
    import asyncio

    job_id = bj._preview_job_id("p1", "h1")
    answers = await _fail_every_run(
        arq_like, job_id, _raised(asyncio.CancelledError()), runs=bj.RETRY_LIMITS["raised"] + 3
    )

    assert {a.status for a in answers} == {"queued"}
    assert arq_like.counters == {}


# -- a raised job is reported for what it raised ------------------------------------

#: Built lazily: ``SandboxUnavailableError`` is the job's own wrapper for a sandbox it
#: never got.
RAISED_READS = {
    "arq_timeout": (lambda: TimeoutError(), "timed_out:job_timeout"),
    "max_tries_after_capacity": (
        lambda: JobExecutionFailed("max 7 retries exceeded"),
        "sandbox_unavailable:capacity",
    ),
    "no_sandbox": (
        lambda: bj.SandboxUnavailableError("RuntimeError: Daytona is not configured"),
        "sandbox_unavailable:no_sandbox",
    ),
    "aborted": (lambda: asyncio.CancelledError(), bj.SUPERSEDED_REASON),
    "code_bug": (lambda: KeyError("layers"), "job_crashed:unhandled_exception"),
    "runtime_error": (lambda: RuntimeError("boom"), "job_crashed:unhandled_exception"),
    # arq's other JobExecutionFailed (an expired job, an unregistered function) is not a
    # capacity wait, so it is not dressed up as one.
    "job_expired": (lambda: JobExecutionFailed("job expired"), "job_crashed:unhandled_exception"),
}


@pytest.mark.parametrize("case", list(RAISED_READS))
async def test_a_raised_job_reads_as_what_it_raised(arq_like: _ArqLike, case: str) -> None:
    make, reason = RAISED_READS[case]
    job_id = bj._preview_job_id("p1", "h1")
    arq_like.finish(job_id, _raised(make()))

    assert await bj._preview_job_outcome(arq_like, job_id) == ("failed", reason)


async def test_a_crash_is_ours_and_never_reads_as_an_unreachable_service(
    arq_like: _ArqLike,
) -> None:
    """The production bug: a job that crashed was told to the user as "the build
    service could not be reached". A crash is not a sandbox we never got."""
    job_id = bj._preview_job_id("p1", "h1")
    arq_like.finish(job_id, _raised(RuntimeError("boom")))

    _status, reason = await bj._preview_job_outcome(arq_like, job_id)

    assert not reason.startswith(bj.RUNG_SANDBOX_UNAVAILABLE)
    assert reason.split(":", 1)[0] == bj.RUNG_JOB_CRASHED
    assert capacity.reason_message(reason) == capacity.JOB_CRASHED_MESSAGE


@pytest.mark.parametrize(
    ("case", "kind"),
    [
        ("arq_timeout", "raised"),
        ("code_bug", "raised"),
        ("no_sandbox", "raised"),
        ("max_tries_after_capacity", "sandbox_unavailable"),
        ("job_expired", "raised"),
        ("aborted", "aborted"),
    ],
)
def test_each_raise_spends_the_right_retry_budget(case: str, kind: str) -> None:
    make, _reason = RAISED_READS[case]
    assert bj._retry_kind(_raised(make())) == kind


_SECRET = "sk_live_" + "a1B2c3D4" * 6
_LEAKY = RuntimeError(f"create refused: Authorization: Bearer {_SECRET} api_key=hunter2hunter2")


def _raised_lines(caplog: pytest.LogCaptureFixture, job_id: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and job_id in r.getMessage() and "raised" in r.getMessage()
    ]


async def test_reading_a_raised_job_logs_its_class_and_a_redacted_message_once(
    arq_like: _ArqLike, caplog: pytest.LogCaptureFixture
) -> None:
    """The worker's exception used to vanish: the API side read ``success=False`` and
    kept only a rung. The read now says what was raised, without what must not leak."""
    job_id = bj._preview_job_id("p1", "h1")
    arq_like.finish(job_id, _raised(_LEAKY))
    caplog.set_level(logging.WARNING, logger=bj.logger.name)

    for _ in range(3):
        await bj._preview_job_outcome(arq_like, job_id)

    lines = _raised_lines(caplog, job_id)
    assert len(lines) == 1, [r.getMessage() for r in lines]
    text = lines[0].getMessage()
    assert "RuntimeError" in text
    assert "create refused" in text
    assert _SECRET not in text
    assert "hunter2hunter2" not in text


async def test_a_long_exception_message_is_capped_in_the_log(
    arq_like: _ArqLike, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = bj._preview_job_id("p1", "h1")
    arq_like.finish(job_id, _raised(RuntimeError("x-" * 5000)))
    caplog.set_level(logging.WARNING, logger=bj.logger.name)

    await bj._preview_job_outcome(arq_like, job_id)

    (line,) = _raised_lines(caplog, job_id)
    assert len(line.getMessage()) < 1000


async def test_the_retry_path_logs_the_raise_too_and_the_budget_still_applies(
    arq_like: _ArqLike, caplog: pytest.LogCaptureFixture
) -> None:
    """``_clear_retryable_result`` reads the same result first; it must say what was
    raised as well, or a retried crash is invisible until the budget runs out."""
    caplog.set_level(logging.WARNING, logger=bj.logger.name)
    job_id = bj._preview_job_id("p1", "h1")
    limit = bj.RETRY_LIMITS["raised"]

    answers = await _fail_every_run(arq_like, job_id, _raised(_LEAKY), runs=limit + 2)

    assert len(arq_like.enqueued) == limit
    assert answers[-1].status == "failed"
    assert answers[-1].reason == "job_crashed:unhandled_exception"
    lines = _raised_lines(caplog, job_id)
    assert lines, "no WARNING named the raised job"
    for line in lines:
        assert "RuntimeError" in line.getMessage()
        assert _SECRET not in line.getMessage()


async def test_clearing_a_raised_result_for_a_retry_logs_what_was_raised(
    arq_like: _ArqLike, caplog: pytest.LogCaptureFixture
) -> None:
    """A retried crash never reaches ``_preview_job_outcome`` (the render is simply
    queued again), so the clear is the only read that can say what the job raised."""
    caplog.set_level(logging.WARNING, logger=bj.logger.name)
    job_id = bj._preview_job_id("p1", "h1")
    arq_like.finish(job_id, _raised(_LEAKY))

    assert await bj._clear_retryable_result(arq_like, job_id) is True

    (line,) = _raised_lines(caplog, job_id)
    assert "RuntimeError" in line.getMessage()
    assert "job_crashed:unhandled_exception" in line.getMessage()
    assert _SECRET not in line.getMessage()
