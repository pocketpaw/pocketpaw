# tests/cloud/test_worker_supervisor.py
#
# Created 2026-09-04 (fix/queue-lanes, backend-perf C1). Splitting site builds onto their
# own arq queue is only half a lane; the other half is a consumer, and a queue with no
# consumer is worse than a shared one because the job waits forever and nothing anywhere
# says so. The supervisor is that consumer side: it runs the chat lane and the site-build
# lane as two arq Workers in one process, because the Coolify compose file cannot take a
# third service that declares ``build:``.
#
# The failure this file exists to prevent is the quiet one. ``sh -c 'arq A & exec arq B'``
# would also run two lanes, and when lane A died the container would stay up and healthy
# while site builds silently stopped being consumed. So the contract under test is: ANY
# lane ending is fatal to the process, whether it raised or returned cleanly.
"""The multi-lane arq supervisor (backend-perf C1)."""

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud import worker_supervisor as sup

pytestmark = pytest.mark.asyncio


class _FakeWorker:
    """Stands in for an arq ``Worker``: it runs a coroutine and records its close."""

    def __init__(self, run, *, close_error: BaseException | None = None) -> None:
        self._run = run
        self._close_error = close_error
        self.closed = False

    async def async_run(self) -> None:
        await self._run()

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _settings(queue: str) -> type:
    return type("WorkerSettings", (), {"queue_name": queue})


def _patch_workers(monkeypatch, workers: list[_FakeWorker]) -> list[dict]:
    """Hand ``run_lanes`` our fake workers and record how it built them."""
    made: list[dict] = []
    it = iter(workers)

    def _create(cls, **kwargs):
        made.append({"settings": cls, "kwargs": kwargs})
        return next(it)

    monkeypatch.setattr(sup, "create_worker", _create)
    return made


async def _forever() -> None:
    await asyncio.Event().wait()


#: How long a supervisor gets to return before the test calls it hung.
#:
#: Generous, because every lane here is a stub and none of them do real work. The
#: deadline is not a performance assertion — it exists because "the supervisor never
#: returns" is a real failure mode, and without a deadline it presents as a test run
#: that hangs rather than one that goes red. A mutation deleting the stop-handler call
#: produces exactly that, and a gate that hangs instead of failing is not a gate.
_DEADLINE_SECONDS = 5.0


async def _run(settings_classes: list[type]) -> int:
    try:
        return await asyncio.wait_for(sup.run_lanes(settings_classes), _DEADLINE_SECONDS)
    except TimeoutError:
        pytest.fail(
            f"run_lanes did not return within {_DEADLINE_SECONDS}s: nothing stopped the lanes"
        )


# --- a lane ending is fatal -------------------------------------------------


async def test_a_lane_that_returns_on_its_own_stops_the_process(monkeypatch):
    """A clean return is just as wrong as a crash.

    ``Worker.async_run`` is not supposed to return while the process is meant to be
    serving. Reporting that as success is how a container stays up with a dead lane --
    exactly the silent half-death the supervisor exists to make impossible.
    """

    async def _returns() -> None:
        return None

    workers = [_FakeWorker(_returns), _FakeWorker(_forever)]
    _patch_workers(monkeypatch, workers)

    code = await _run([_settings("a"), _settings("b")])

    assert code == 1


async def test_a_lane_that_raises_stops_the_process(monkeypatch):
    async def _raises() -> None:
        raise RuntimeError("redis went away")

    workers = [_FakeWorker(_forever), _FakeWorker(_raises)]
    _patch_workers(monkeypatch, workers)

    code = await _run([_settings("a"), _settings("b")])

    assert code == 1


async def test_the_surviving_lane_is_stopped_and_closed_too(monkeypatch):
    """One lane down means the process goes down, not that it limps on with one lane.

    Leaving the survivor running would keep the container alive and healthy-looking while
    half the background work quietly stopped happening.
    """

    async def _returns() -> None:
        return None

    survivor = _FakeWorker(_forever)
    workers = [_FakeWorker(_returns), survivor]
    _patch_workers(monkeypatch, workers)

    await _run([_settings("a"), _settings("b")])

    assert survivor.closed is True


# --- a signal is the only clean stop ----------------------------------------


async def test_a_stop_signal_exits_clean(monkeypatch):
    """SIGTERM from the container runtime is the one ending that is not a failure."""

    def _install(stop: asyncio.Event) -> None:
        asyncio.get_running_loop().call_soon(stop.set)

    monkeypatch.setattr(sup, "_install_stop_handlers", _install)
    workers = [_FakeWorker(_forever), _FakeWorker(_forever)]
    _patch_workers(monkeypatch, workers)

    code = await _run([_settings("a"), _settings("b")])

    assert code == 0
    assert all(w.closed for w in workers)


async def test_the_supervisor_asks_for_the_stop_signal_at_all(monkeypatch):
    """Without this the deployed container would only ever stop by being killed.

    arq's own handler is switched off deliberately (see below), so if nobody installs a
    replacement, SIGTERM does nothing and Docker escalates to SIGKILL after its grace
    period — terminating in-flight jobs instead of draining them.
    """
    seen: list[asyncio.Event] = []

    def _install(stop: asyncio.Event) -> None:
        seen.append(stop)
        asyncio.get_running_loop().call_soon(stop.set)

    monkeypatch.setattr(sup, "_install_stop_handlers", _install)
    _patch_workers(monkeypatch, [_FakeWorker(_forever)])

    await _run([_settings("a")])

    assert len(seen) == 1 and isinstance(seen[0], asyncio.Event)


# --- how the workers are built ----------------------------------------------


async def test_every_lane_is_built_with_arqs_own_signal_handling_off(monkeypatch):
    """Two arq Workers on one loop would fight over the signal handler.

    ``loop.add_signal_handler`` replaces rather than appends, so the last Worker to
    register would silently become the only one that ever hears SIGTERM, and the other
    lane would never drain. The supervisor owns the signal instead.
    """

    def _install(stop: asyncio.Event) -> None:
        asyncio.get_running_loop().call_soon(stop.set)

    monkeypatch.setattr(sup, "_install_stop_handlers", _install)
    workers = [_FakeWorker(_forever), _FakeWorker(_forever)]
    made = _patch_workers(monkeypatch, workers)

    await _run([_settings("a"), _settings("b")])

    assert [m["kwargs"]["handle_signals"] for m in made] == [False, False]


async def test_a_close_that_raises_does_not_take_the_shutdown_with_it(monkeypatch):
    """Shutdown paths cannot afford to raise.

    ``Worker.close`` calls ``handle_sig(signal.SIGUSR1)`` when signals were handed to us
    instead, and SIGUSR1 does not exist on Windows — so this raises on a developer
    machine for a reason unrelated to the code. It can also raise against a Redis that has
    already gone, which is precisely when there is nothing useful to do about it.
    """

    def _install(stop: asyncio.Event) -> None:
        asyncio.get_running_loop().call_soon(stop.set)

    monkeypatch.setattr(sup, "_install_stop_handlers", _install)
    angry = _FakeWorker(_forever, close_error=AttributeError("SIGUSR1"))
    calm = _FakeWorker(_forever)
    _patch_workers(monkeypatch, [angry, calm])

    code = await _run([_settings("a"), _settings("b")])

    assert code == 0
    assert calm.closed is True


async def test_starting_with_no_lanes_is_an_error_not_an_idle_process():
    """A supervisor with nothing to supervise would sit there looking healthy forever.

    Under the same deadline as every other call here, and for a sharper reason: without
    the guard this does not raise, it waits on a stop event that no lane will ever cause.
    That is the failure being asserted, so it has to arrive as a red test rather than as
    a run that never finishes.
    """
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(sup.run_lanes([]), _DEADLINE_SECONDS)


# --- which lanes a deployment actually runs ---------------------------------


async def test_the_deployed_lanes_are_chat_and_site_builds(monkeypatch):
    """Without the growth flag, only the chat and site-build lanes run.

    The growth lane executes live outbound sends, so it must never start by default.
    """
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("POCKETPAW_GROWTH_WORKER_ENABLED", raising=False)
    from arq.constants import default_queue_name
    from pocketpaw_ee.sites.build_job import SITE_BUILD_QUEUE_NAME

    names = [sup.lane_name(cls) for cls in sup.default_lanes()]

    assert names == [default_queue_name, SITE_BUILD_QUEUE_NAME]


async def test_the_growth_flag_adds_the_growth_lane(monkeypatch):
    """The flag that lets the web process enqueue growth sends also starts their consumer."""
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("POCKETPAW_GROWTH_WORKER_ENABLED", "true")
    from arq.constants import default_queue_name
    from pocketpaw_ee.cloud.growth.domain import GROWTH_QUEUE_NAME
    from pocketpaw_ee.sites.build_job import SITE_BUILD_QUEUE_NAME

    names = [sup.lane_name(cls) for cls in sup.default_lanes()]

    assert names == [default_queue_name, SITE_BUILD_QUEUE_NAME, GROWTH_QUEUE_NAME]


async def test_a_lane_without_its_own_queue_is_named_after_arqs_default():
    """Every settings class in this codebase is called ``WorkerSettings``.

    A log line naming the class would identify nothing; the queue is the only thing that
    tells two lanes apart in an incident.
    """
    from arq.constants import default_queue_name

    assert sup.lane_name(type("WorkerSettings", (), {})) == default_queue_name
    assert sup.lane_name(_settings("arq:queue:sites")) == "arq:queue:sites"


# --- SIGTERM drains in-flight jobs before stopping --------------------------
#
# A deploy recreates the worker container. Docker sends SIGTERM, and before the drain
# existed the supervisor went straight to ``Worker.close()``, whose ``handle_sig``
# cancels every running job on the spot: each in-flight chat run ended ``interrupted``
# mid-reply on every deploy. The fakes below behave like arq on the two points that
# matter: ``close()`` cancels whatever is still in ``tasks``, and ``allow_pick_jobs`` is
# the switch that stops the poll loop claiming new work.


class _ArqLikeWorker(_FakeWorker):
    """A lane with one in-flight job, closed the way arq closes: cancel what is left."""

    def __init__(self, job) -> None:
        super().__init__(_forever)
        self.allow_pick_jobs = True
        self.tasks: dict[str, asyncio.Task] = {}
        self._job = job

    async def async_run(self) -> None:
        self.tasks["job-1"] = asyncio.create_task(self._job(self))
        await _forever()

    async def close(self) -> None:
        self.closed = True
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def _stop_soon(monkeypatch, delay: float = 0.05) -> None:
    def _install(stop: asyncio.Event) -> None:
        asyncio.get_running_loop().call_later(delay, stop.set)

    monkeypatch.setattr(sup, "_install_stop_handlers", _install)


@pytest.fixture
def _shutdown_flag():
    from pocketpaw_ee.cloud._core import shutdown

    shutdown.reset()
    yield shutdown
    shutdown.reset()


async def test_a_stop_signal_lets_the_in_flight_job_finish(monkeypatch, _shutdown_flag):
    """The deploy bug: SIGTERM cancelled a running chat reply instead of waiting for it."""
    monkeypatch.setenv("POCKETPAW_WORKER_DRAIN_SECONDS", "5")
    outcome: list[str] = []

    async def job(worker) -> None:  # noqa: ARG001
        try:
            await asyncio.sleep(0.3)
            outcome.append("finished")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    _stop_soon(monkeypatch)
    worker = _ArqLikeWorker(job)
    _patch_workers(monkeypatch, [worker])

    code = await _run([_settings("a")])

    assert outcome == ["finished"], "SIGTERM cancelled the in-flight job instead of draining it"
    assert code == 0
    assert worker.closed is True


async def test_a_draining_lane_stops_taking_new_jobs(monkeypatch, _shutdown_flag):
    """Draining only helps if the lane stops claiming work it would then have to abandon."""
    monkeypatch.setenv("POCKETPAW_WORKER_DRAIN_SECONDS", "5")
    seen: list[bool] = []

    async def job(worker) -> None:
        await asyncio.sleep(0.3)
        seen.append(worker.allow_pick_jobs)

    _stop_soon(monkeypatch)
    _patch_workers(monkeypatch, [_ArqLikeWorker(job)])

    await _run([_settings("a")])

    assert seen == [False]


async def test_the_drain_is_bounded_and_flags_the_shutdown(monkeypatch, _shutdown_flag):
    """A job that outlives the grace window is cancelled, and it can tell why.

    The run's cancel cleanup reads the flag to tell "the worker is going away, retry me"
    from "my own job_timeout fired, give up", so it must be set before the cancel lands.
    """
    monkeypatch.setenv("POCKETPAW_WORKER_DRAIN_SECONDS", "0.2")
    flagged_at_cancel: list[bool] = []

    async def job(worker) -> None:  # noqa: ARG001
        try:
            await _forever()
        except asyncio.CancelledError:
            flagged_at_cancel.append(_shutdown_flag.is_stopping())
            raise

    _stop_soon(monkeypatch)
    _patch_workers(monkeypatch, [_ArqLikeWorker(job)])

    code = await _run([_settings("a")])

    assert code == 0
    assert flagged_at_cancel == [True]


async def test_a_zero_drain_window_cancels_at_once(monkeypatch, _shutdown_flag):
    """``0`` keeps the old instant stop for anyone who wants it (local dev)."""
    monkeypatch.setenv("POCKETPAW_WORKER_DRAIN_SECONDS", "0")
    outcome: list[str] = []

    async def job(worker) -> None:  # noqa: ARG001
        try:
            await asyncio.sleep(2)
            outcome.append("finished")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    _stop_soon(monkeypatch)
    _patch_workers(monkeypatch, [_ArqLikeWorker(job)])

    await _run([_settings("a")])

    assert outcome == ["cancelled"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 300.0), ("120", 120.0), ("0", 0.0), ("-5", 300.0), ("soon", 300.0)],
)
async def test_the_drain_window_reads_its_env_fail_soft(monkeypatch, raw, expected):
    monkeypatch.setenv("POCKETPAW_WORKER_DRAIN_SECONDS", raw)

    assert sup.drain_seconds() == expected
