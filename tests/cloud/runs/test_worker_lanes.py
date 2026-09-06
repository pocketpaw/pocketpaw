# tests/cloud/runs/test_worker_lanes.py
#
# Created 2026-09-04 (fix/queue-lanes, backend-perf C1). Site builds used to ride arq's
# default queue alongside chat runs, workspace jobs and both /ship jobs, which meant one
# ``max_jobs`` ceiling — default 10 — for every lane the product runs. Ten concurrent
# publishes left chat with zero slots, and the request that lost did not error: it waited
# in Redis behind a 30-minute ``job_timeout`` while the user watched an SSE stream deliver
# only heartbeats, until the stale-run sweeper called it interrupted ten minutes later.
#
# These tests pin the three things that make the split real rather than cosmetic: the
# sites lane reads a DIFFERENT queue, its ceiling answers to its OWN variable, and the two
# lanes share one bootstrap without running it twice.
"""The site-build lane's separation from the chat lane (backend-perf C1)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from arq.connections import RedisSettings
from arq.constants import default_queue_name
from pocketpaw_ee.cloud.chat.runs import worker as chat_worker
from pocketpaw_ee.sites import build_worker as sites_worker
from pocketpaw_ee.sites.build_job import SITE_BUILD_QUEUE_NAME

pytestmark = pytest.mark.asyncio


# --- the lanes are actually separate ---------------------------------------


async def test_the_sites_lane_reads_a_different_queue_than_the_chat_lane():
    """The whole fix in one assertion.

    ``queue_name`` decides which Redis list a Worker polls, and therefore which
    ``max_jobs`` semaphore a job competes for. Same queue, same ceiling, and the split is
    two settings classes describing one lane.
    """
    chat_queue = getattr(chat_worker.WorkerSettings, "queue_name", default_queue_name)

    assert sites_worker.WorkerSettings.queue_name == SITE_BUILD_QUEUE_NAME
    assert sites_worker.WorkerSettings.queue_name != chat_queue


async def test_the_sites_ceiling_answers_to_its_own_variable(monkeypatch):
    """Raising the chat ceiling must not raise the build ceiling with it.

    They are bounded by different things — container RAM for chat, which spawns a Node
    subprocess per run, and the third-party sandbox quota for builds — so one variable
    for both would make surviving a busy morning also mean paying for more sandboxes.
    """
    monkeypatch.setenv("POCKETPAW_ARQ_MAX_JOBS", "40")
    monkeypatch.delenv("POCKETPAW_SITES_ARQ_MAX_JOBS", raising=False)

    assert chat_worker._max_jobs() == 40
    assert sites_worker._sites_max_jobs() == sites_worker._DEFAULT_SITES_MAX_JOBS


@pytest.mark.parametrize("raw", ["", "   ", "not-an-int", "0", "-3"])
async def test_an_unusable_sites_ceiling_falls_back_instead_of_reaching_arq(monkeypatch, raw):
    """Fail-soft, for the same reason the chat lane is.

    ``0`` wedges the Worker into accepting nothing at all and a negative crashes
    ``BoundedSemaphore`` on boot, so a typo in a deploy variable would take the lane down
    rather than mis-tune it.
    """
    monkeypatch.setenv("POCKETPAW_SITES_ARQ_MAX_JOBS", raw)

    assert sites_worker._sites_max_jobs() == sites_worker._DEFAULT_SITES_MAX_JOBS


async def test_a_usable_sites_ceiling_is_honoured(monkeypatch):
    monkeypatch.setenv("POCKETPAW_SITES_ARQ_MAX_JOBS", "7")

    assert sites_worker._sites_max_jobs() == 7


async def test_the_default_ceilings_leave_room_for_chat():
    """The build lane must not be able to claim the chat lane's capacity by default.

    This is the number the finding was actually about. A default equal to the chat
    ceiling would restore the starvation the split removes, just with two queues.
    """
    assert 0 < sites_worker._DEFAULT_SITES_MAX_JOBS < chat_worker._DEFAULT_MAX_JOBS


# --- one definition of a build, two lanes that can run it -------------------


async def test_the_sites_lane_registers_the_chat_lanes_own_wrapped_functions():
    """Identity, not equality.

    Each site function is wrapped with its own timeout because a build's budget (1020s at
    today's defaults) is wider than the workspace-jobs timeout it used to share. A second
    wrapping here would fork that number, and an arq cancellation that lands before the
    in-sandbox timeout fires destroys the sentinel the lane classifies from — a slow but
    healthy build gets recorded as lost infrastructure.
    """
    assert sites_worker.WorkerSettings.functions == [
        chat_worker.site_build_fn,
        chat_worker.site_preview_build_fn,
    ]


async def test_the_chat_lane_still_claims_site_jobs_left_on_the_default_queue():
    """The cutover has a backlog, and it needs someone willing to claim it.

    A deploy swaps the enqueue side instantly. Anything already sitting on the default
    queue when the old process stopped would otherwise wait forever with no worker
    registered for its name — and arq says nothing about a job nobody claims.
    """
    names = {
        getattr(f, "name", getattr(f, "__name__", "")) for f in chat_worker.WorkerSettings.functions
    }

    assert {"run_site_build", "run_site_preview_build"} <= names


async def test_the_sites_settings_survive_arqs_dunder_dict_read():
    """arq's ``get_kwargs`` reads ``settings_cls.__dict__`` directly.

    That bypasses the descriptor protocol, so anything lazy here is handed to the Worker
    as-is and crashes when arq tries to use it. Same contract the chat lane already pins.
    """
    d = sites_worker.WorkerSettings.__dict__

    assert isinstance(d["redis_settings"], RedisSettings)
    assert isinstance(d["max_jobs"], int)
    assert isinstance(d["job_timeout"], int)
    assert isinstance(d["health_check_interval"], int)


# --- one bootstrap, however many lanes share the process --------------------


class TestTheSharedBootstrap:
    """``on_startup`` runs once per Worker; the process only wants it once.

    The lane counter is reset around every test in this directory by an autouse fixture
    in ``conftest.py`` — it is process state, and a test session is one process.
    """

    async def test_the_second_lane_does_not_bootstrap_again(self, monkeypatch):
        """Two lanes, one ``init_cloud_db`` and one pass of the boot sweeps.

        The compute-cost metering sweep and the LiteLLM billing cutover sweep both run in
        there. They are idempotent by ledger key, so a second pass would waste work rather
        than double charge — but a billing path is not where "probably harmless" is the
        standard, and a second ``init_cloud_db`` against a live client is not something to
        learn about in production.
        """
        boot = AsyncMock()
        monkeypatch.setattr(chat_worker, "_bootstrap", boot)

        await chat_worker._startup({})
        await chat_worker._startup({})

        assert boot.await_count == 1

    async def test_the_database_closes_only_when_the_last_lane_stops(self, monkeypatch):
        """A lane stopping is not the process stopping.

        Closing the shared Mongo client on the first ``on_shutdown`` would pull the
        database out from under every job the other lane still has in flight.
        """
        monkeypatch.setattr(chat_worker, "_bootstrap", AsyncMock())
        close = AsyncMock()
        monkeypatch.setattr(chat_worker, "close_cloud_db", close)

        await chat_worker._startup({})
        await chat_worker._startup({})
        await chat_worker._shutdown({})
        assert close.await_count == 0

        await chat_worker._shutdown({})
        assert close.await_count == 1

    async def test_an_unbalanced_shutdown_cannot_break_the_next_bootstrap(self, monkeypatch):
        """arq calls ``on_shutdown`` even for a lane whose ``on_startup`` raised.

        Without the clamp that drives the counter NEGATIVE, and a negative counter makes
        the guard under-count: the process comes back up, and because -1 + 1 is still not
        greater than 1, the SECOND lane bootstraps as well. That is the double
        ``init_cloud_db`` and the double billing sweep this guard exists to prevent,
        arrived at from the other direction.

        Two startups is the whole point of the test. One would pass either way, which is
        how the first version of it let the mutation escape.
        """
        boot = AsyncMock()
        monkeypatch.setattr(chat_worker, "_bootstrap", boot)
        monkeypatch.setattr(chat_worker, "close_cloud_db", AsyncMock())

        await chat_worker._shutdown({})
        await chat_worker._shutdown({})

        await chat_worker._startup({})
        await chat_worker._startup({})

        assert boot.await_count == 1
