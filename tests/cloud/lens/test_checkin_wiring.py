# tests/cloud/lens/test_checkin_wiring.py — ee sites that check in to paw-lens.
#
# Proves the 5-minute ``_run_sweeps`` pass checks in once per sweep (and a
# raising sweep posts ``error`` without stopping the rest), ``sweep_tick``
# builds the schedule paw-lens registers, and the arq supervisor wraps every
# lane's job and cron functions without renaming them, leaving chat runs alone.
"""paw-lens check-in wiring in pocketpaw_ee."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from pocketpaw import lens_checkins


@pytest.fixture
def seen(monkeypatch) -> list[dict]:
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202)

    monkeypatch.setattr(lens_checkins, "_transport", httpx.MockTransport(handle))
    monkeypatch.setattr(lens_checkins, "_client", None)
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(lens_api_url="http://lens:8790", lens_api_token="t"),
    )
    return bodies


async def test_run_sweeps_checks_in_per_sweep(monkeypatch, seen):
    from pocketpaw_ee import extensions

    async def sweep_good():
        return None

    async def sweep_bad():
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(extensions, "_sweeps", lambda: ([sweep_bad, sweep_good], []))
    monkeypatch.setattr(extensions, "_sweep_lease", None)
    monkeypatch.setattr(extensions, "_jail_lease", None)
    monkeypatch.setenv("POCKETPAW_SWEEPER_INTERVAL_SECONDS", "600")

    await extensions._run_sweeps()
    await lens_checkins.flush()

    by_monitor: dict[str, list[str]] = {}
    for body in seen:
        by_monitor.setdefault(body["monitor"], []).append(body["status"])
        assert body["kind"] == "sweep"
        assert "interval_seconds" in body["schedule"]
    assert by_monitor == {
        "sweep:sweep_bad": ["in_progress", "error"],
        "sweep:sweep_good": ["in_progress", "ok"],
    }


def test_sweep_tick_schedule_shapes(monkeypatch):
    from pocketpaw_ee.cloud._core import periodic

    calls = []
    monkeypatch.setattr(periodic, "automation_run", lambda *a, **kw: calls.append((a, kw)))
    periodic.sweep_tick("a", 300)
    periodic.sweep_tick("b", crontab="0 0 * * *")
    periodic.sweep_tick("c")
    periodic.sweep_tick("d", 0.5)
    periodic.sweep_tick("e", 2.6)
    assert [c[1]["schedule"] for c in calls] == [
        {"interval_seconds": 300},
        {"crontab": "0 0 * * *"},
        None,
        None,
        {"interval_seconds": 3},
    ]
    assert all(c[0][0] == "sweep" for c in calls)


async def execute_run_job(ctx, spec):  # same qualname as the real chat-run job
    return None


async def test_arq_lanes_are_wrapped_without_renaming(seen):
    from arq import cron, func
    from pocketpaw_ee.cloud.worker_supervisor import _monitored

    ran = []

    async def site_job(ctx, x):
        ran.append(x)
        return x

    async def nightly(ctx):
        ran.append("cron")

    class Lane:
        functions = [execute_run_job, func(site_job, name="run_site_build", timeout=99)]
        cron_jobs = [cron(nightly, name="growth.nightly", hour=3)]

    out = _monitored(Lane)
    chat, build = out["functions"]
    assert chat.coroutine is execute_run_job, "interactive chat runs are not automations"
    assert (build.name, build.timeout_s) == ("run_site_build", 99)
    assert build.coroutine.__lens_monitor__ == ("job", "run_site_build")
    (nightly_job,) = out["cron_jobs"]
    assert nightly_job.name == "growth.nightly"

    assert await build.coroutine({}, "built") == "built"
    await nightly_job.coroutine({})
    await lens_checkins.flush()
    assert ran == ["built", "cron"]
    assert [(b["monitor"], b["status"]) for b in seen] == [
        ("job:run_site_build", "in_progress"),
        ("job:run_site_build", "ok"),
        ("job:growth.nightly", "in_progress"),
        ("job:growth.nightly", "ok"),
    ]
