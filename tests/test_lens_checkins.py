# tests/test_lens_checkins.py — the paw-lens check-in helper and its wiring.
#
# Covers the check-in pair, the error path (re-raise unchanged), paw-lens being
# down / slow / 500 (job unaffected, bounded), disabled (zero requests), the
# token header, baggage reaching a child span, and that every OSS APScheduler
# site hands add_job a monitored function.
"""Tests for pocketpaw.lens_checkins."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from pocketpaw import lens_checkins
from pocketpaw.lens_checkins import automation_run, flush, monitored_job, schedule_from_trigger


@pytest.fixture
def lens(monkeypatch):
    """Point the helper at a fake paw-lens; returns (seen bodies, set_handler)."""
    seen: list[dict] = []
    state = {"handler": lambda req: httpx.Response(202)}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "token": request.headers.get("X-Lens-Token"),
                "body": json.loads(request.content),
            }
        )
        return state["handler"](request)

    monkeypatch.setattr(lens_checkins, "_transport", httpx.MockTransport(handle))
    monkeypatch.setattr(lens_checkins, "_client", None)
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(lens_api_url="http://lens:8790/", lens_api_token="s3cret"),
    )

    def set_handler(fn):
        state["handler"] = fn

    return seen, set_handler


async def test_ok_run_posts_in_progress_then_ok(lens):
    seen, _ = lens
    async with automation_run("reminder", "r1", schedule={"crontab": "0 9 * * *"}):
        pass
    await flush()
    assert [s["body"]["status"] for s in seen] == ["in_progress", "ok"]
    first, last = seen[0]["body"], seen[1]["body"]
    assert first["monitor"] == "reminder:r1" and first["kind"] == "reminder"
    assert first["schedule"] == {"crontab": "0 9 * * *"}
    assert first["checkin_id"] == last["checkin_id"]
    assert seen[0]["url"] == "http://lens:8790/v1/checkins"
    assert {s["token"] for s in seen} == {"s3cret"}


async def test_error_run_posts_error_and_reraises_original(lens):
    seen, _ = lens
    boom = ValueError("db exploded")
    with pytest.raises(ValueError) as caught:
        async with automation_run("sweep", "x"):
            raise boom
    assert caught.value is boom
    await flush()
    assert [s["body"]["status"] for s in seen] == ["in_progress", "error"]
    assert "db exploded" in seen[1]["body"]["error"]


async def test_error_text_is_truncated(lens):
    seen, _ = lens
    with pytest.raises(RuntimeError):
        async with automation_run("sweep", "x"):
            raise RuntimeError("y" * 5000)
    await flush()
    assert len(seen[1]["body"]["error"]) <= lens_checkins.MAX_ERROR_CHARS


@pytest.mark.parametrize("mode", ["down", "500", "hang"])
async def test_lens_failure_never_reaches_the_job(lens, mode):
    seen, set_handler = lens

    async def hang(request):
        await asyncio.sleep(30)
        return httpx.Response(202)

    if mode == "down":
        set_handler(lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    elif mode == "500":
        set_handler(lambda req: httpx.Response(500))
    else:
        # MockTransport supports async handlers through the async client.
        lens_checkins._transport = httpx.MockTransport(hang)

    ran = []
    started = time.monotonic()
    async with automation_run("job", "j"):
        ran.append(True)
    assert time.monotonic() - started < 0.2, "the job must not wait on paw-lens"
    await flush(timeout=3)
    assert ran == [True]
    assert time.monotonic() - started < 2.5, "each post is bounded by the 1 s budget"
    assert not lens_checkins._pending


async def test_disabled_makes_zero_requests(lens, monkeypatch):
    seen, _ = lens
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(lens_api_url="", lens_api_token="s3cret"),
    )
    async with automation_run("reminder", "r1"):
        pass
    await flush()
    assert seen == []


async def test_workspace_falls_back_to_current_workspace(lens):
    from pocketpaw.stores import current_workspace

    seen, _ = lens
    token = current_workspace.set("ws-7")
    try:
        async with automation_run("intention", "i1"):
            pass
    finally:
        current_workspace.reset(token)
    await flush()
    assert {s["body"]["workspace_id"] for s in seen} == {"ws-7"}


async def test_baggage_lands_on_child_spans(lens, capfire, monkeypatch):
    import logfire

    async with automation_run("mandate", "m1", workspace_id="ws-1"):
        with logfire.span("child"):
            pass
    await flush()
    child = next(s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "child")
    attrs = child["attributes"]
    assert attrs["paw.workspace_id"] == "ws-1"
    assert attrs["paw.automation.kind"] == "mandate"
    assert attrs["paw.automation.id"] == "m1"


def test_schedule_from_trigger():
    assert schedule_from_trigger(CronTrigger(minute="0", hour="9", day_of_week="mon-fri")) == {
        "crontab": "0 9 * * mon-fri"
    }
    assert schedule_from_trigger(IntervalTrigger(minutes=5)) == {"interval_seconds": 300}
    assert schedule_from_trigger(DateTrigger(run_date="2030-01-01")) is None
    assert schedule_from_trigger(None) is None


async def test_monitored_job_checks_in_with_trigger_schedule(lens):
    seen, _ = lens
    calls = []

    async def job(a, b=0):
        calls.append((a, b))
        return "done"

    wrapped = monitored_job(job, kind="heartbeat", id="hb", trigger=IntervalTrigger(minutes=2))
    assert wrapped.__lens_monitor__ == ("heartbeat", "hb")
    assert await wrapped(1, b=2) == "done"
    await flush()
    assert calls == [(1, 2)]
    assert seen[0]["body"]["schedule"] == {"interval_seconds": 120}


# --- wiring: every OSS APScheduler site hands add_job a monitored function ----


def _monitors(scheduler) -> dict[str, tuple[str, str] | None]:
    return {job.id: getattr(job.func, "__lens_monitor__", None) for job in scheduler.get_jobs()}


def test_reminder_jobs_are_monitored(monkeypatch, tmp_path):
    from pocketpaw import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "save_reminders", lambda r: None)
    rs = sched_mod.ReminderScheduler()
    rs._add_recurring_job({"id": "rec", "schedule": "0 9 * * *"})
    rs._add_job({"id": "one", "trigger_at": "2099-01-01T00:00:00+00:00"})
    monitors = _monitors(rs.scheduler)
    assert monitors["rec"] == ("reminder", "rec")
    assert monitors["one"] == ("reminder", "one")


def test_intention_jobs_are_monitored():
    from pocketpaw.daemon.triggers import TriggerEngine

    engine = TriggerEngine()
    engine._add_cron_trigger({"id": "i1", "name": "n", "trigger": {"schedule": "0 9 * * *"}})
    engine._add_stale_trigger({"id": "i2", "name": "s", "trigger": {}})
    monitors = _monitors(engine.scheduler)
    assert monitors["intention_i1"] == ("intention", "i1")
    assert monitors["intention_i2"] == ("intention", "i2")


def test_heartbeat_job_is_monitored():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from pocketpaw.mission_control.heartbeat import HeartbeatDaemon

    sched = AsyncIOScheduler()
    daemon = HeartbeatDaemon(scheduler=sched)
    daemon._owns_scheduler = False
    daemon.start()
    try:
        assert set(_monitors(sched).values()) == {("heartbeat", daemon._job_id)}
    finally:
        daemon.stop()
