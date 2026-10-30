# tests/ee/sites/test_admission_job_timeout.py — the arq timeouts the site lanes
# register at import and the ``site_*_job_timeout_seconds()`` callers read later must be
# the same number whether or not admission (``PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB``) is
# on. The registrations are evaluated once per process; if the formula read the
# admission env var, a test (or worker) that flipped it between import and call would
# see two different timeouts. Parametrized over both env states so the check holds in
# whichever order xdist schedules it.
from __future__ import annotations

import math

import pytest
from pocketpaw_ee.sites import admission
from pocketpaw_ee.sites import build_job as bj


@pytest.fixture(params=["unset", "set"])
def admission_env(request, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "set":
        monkeypatch.setenv(admission.BUDGET_ENV, "10")
    else:
        monkeypatch.delenv(admission.BUDGET_ENV, raising=False)
    # build_worker builds its arq settings at import; same pattern as test_delete_job.py.
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379")
    return request.param


def test_the_timeout_does_not_depend_on_the_admission_env(
    admission_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = (bj.site_build_job_timeout_seconds(), bj.site_preview_job_timeout_seconds())
    if admission_env == "set":
        monkeypatch.delenv(admission.BUDGET_ENV)
    else:
        monkeypatch.setenv(admission.BUDGET_ENV, "10")
    other = (bj.site_build_job_timeout_seconds(), bj.site_preview_job_timeout_seconds())
    assert here == other


def test_the_timeout_always_covers_the_admission_wait(admission_env: str) -> None:
    from pocketpaw_ee.sites.daytona_build import resolve_build_timeout_seconds
    from pocketpaw_ee.sites.daytona_runner import EXEC_TIMEOUT_SLACK_SECONDS

    widest = max(resolve_build_timeout_seconds(e) for e in bj.BUILDABLE_ENGINES)
    assert bj.site_build_job_timeout_seconds() == (
        widest
        + EXEC_TIMEOUT_SLACK_SECONDS
        + bj.OUT_OF_SANDBOX_MARGIN_SECONDS
        + math.ceil(admission.MAX_WAIT_SECONDS)
    )


def test_registered_arq_timeouts_match_the_functions(admission_env: str) -> None:
    from pocketpaw_ee.cloud.chat.runs import worker as chat_worker
    from pocketpaw_ee.sites import build_worker

    assert chat_worker.site_build_fn.timeout_s == bj.site_build_job_timeout_seconds()
    assert chat_worker.site_preview_build_fn.timeout_s == bj.site_preview_job_timeout_seconds()
    assert build_worker._site_html_verify_fn.timeout_s == bj.site_preview_job_timeout_seconds()
    assert build_worker.WorkerSettings.job_timeout == bj.site_build_job_timeout_seconds()
