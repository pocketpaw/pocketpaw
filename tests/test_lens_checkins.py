# tests/test_lens_checkins.py — the paw-lens check-in helper and its wiring.
#
# Covers the check-in pair, the error path (re-raise unchanged), paw-lens being
# down / slow / 500 (job unaffected, bounded), disabled (zero requests), the
# token header, run attributes reaching a child span but never a ``baggage``
# header, the tick's trace_id in both check-ins (absent with Logfire off), no
# automation span without a lens URL (child spans still stamped),
# cancellation posting no final check-in, the stateless claude_sdk span
# surviving finalization in another task, and that every OSS APScheduler site
# hands add_job a monitored function.
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


@pytest.fixture
def run_attrs(capfire):
    """capfire reconfigures logfire, so add the processor configure_observability adds."""
    from opentelemetry import trace

    from pocketpaw.observability import run_attributes_processor

    trace.get_tracer_provider().add_span_processor(run_attributes_processor())
    return capfire


async def test_baggage_lands_on_child_spans(lens, run_attrs):
    import logfire

    async with automation_run("mandate", "m1", workspace_id="ws-1"):
        with logfire.span("child"):
            pass
    await flush()
    child = next(s for s in run_attrs.exporter.exported_spans_as_dict() if s["name"] == "child")
    attrs = child["attributes"]
    assert attrs["paw.workspace_id"] == "ws-1"
    assert attrs["paw.automation.kind"] == "mandate"
    assert attrs["paw.automation.id"] == "m1"


async def test_trace_id_sent_and_matches_the_ticks_spans(lens, run_attrs, monkeypatch):
    import logfire

    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "1")
    async with automation_run("mandate", "m1", workspace_id="ws-1"):
        with logfire.span("child"):
            pass
    await flush()
    seen, _ = lens
    trace_ids = {s["body"]["trace_id"] for s in seen}
    assert len(seen) == 2 and len(trace_ids) == 1
    trace_id = trace_ids.pop()
    assert len(trace_id) == 32 and trace_id == trace_id.lower()
    int(trace_id, 16)
    spans = {s["name"]: s for s in run_attrs.exporter.exported_spans_as_dict()}
    tick, child = spans["automation mandate:m1"], spans["child"]
    assert f"{child['context']['trace_id']:032x}" == trace_id
    assert child["parent"]["span_id"] == tick["context"]["span_id"]
    assert tick["attributes"]["paw.automation.id"] == "m1"


async def test_no_automation_span_without_lens_but_baggage_still_set(lens, run_attrs, monkeypatch):
    """No lens URL: the run opens no root span of its own, child spans keep paw.*."""
    import logfire

    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "1")
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(lens_api_url="", lens_api_token=""),
    )
    async with automation_run("mandate", "m1", workspace_id="ws-1"):
        with logfire.span("child"):
            pass
    await flush()
    spans = {s["name"]: s for s in run_attrs.exporter.exported_spans_as_dict()}
    assert "automation mandate:m1" not in spans
    attrs = spans["child"]["attributes"]
    assert attrs["paw.workspace_id"] == "ws-1"
    assert attrs["paw.automation.kind"] == "mandate"
    assert attrs["paw.automation.id"] == "m1"


async def test_no_trace_id_when_logfire_off(lens, monkeypatch):
    monkeypatch.delenv("POCKETPAW_LOGFIRE_ENABLED", raising=False)
    async with automation_run("mandate", "m1"):
        pass
    await flush()
    seen, _ = lens
    assert len(seen) == 2
    assert all("trace_id" not in s["body"] for s in seen)


def test_run_attributes_never_leave_in_a_baggage_header(run_attrs):
    """OTel baggage is injected into every outbound request; ours must not be."""
    import logfire
    from opentelemetry.propagate import inject

    from pocketpaw.observability import baggage

    control: dict[str, str] = {}
    with logfire.set_baggage(probe="x"):
        inject(control)
    assert "baggage" in control, "the global propagator does carry OTel baggage"

    carrier: dict[str, str] = {}
    with baggage(**{"paw.workspace_id": "ws-1"}), baggage(**{"paw.automation.id": "a1"}):
        with logfire.span("outbound"):
            inject(carrier)
    assert "baggage" not in carrier
    span = next(s for s in run_attrs.exporter.exported_spans_as_dict() if s["name"] == "outbound")
    assert span["attributes"]["paw.workspace_id"] == "ws-1"
    assert span["attributes"]["paw.automation.id"] == "a1"


async def test_cancelled_run_posts_no_final_checkin(lens):
    seen, _ = lens
    with pytest.raises(asyncio.CancelledError):
        async with automation_run("sweep", "s1"):
            raise asyncio.CancelledError()
    await flush()
    assert [s["body"]["status"] for s in seen] == ["in_progress"]


@pytest.mark.parametrize(
    ("aps_dow", "crontab_dow"),
    [
        ("mon", "1"),
        ("sun", "0"),
        ("sat", "6"),
        ("mon-fri", "1,2,3,4,5"),
        ("fri-sun", "0,5,6"),  # wraps in crontab, so a list, never "5-0"
        ("mon,wed,fri", "1,3,5"),
        ("0", "1"),  # APScheduler 0 = Monday
        ("6", "0"),  # APScheduler 6 = Sunday
        ("5-6", "0,6"),
        ("0,4", "1,5"),
        ("*/2", "0,1,3,5"),  # mon, wed, fri, sun
        ("*", "*"),
        ("mon-sun", "*"),
    ],
)
def test_day_of_week_is_translated_to_crontab_numbering(aps_dow, crontab_dow):
    trigger = CronTrigger(minute="0", hour="9", day_of_week=aps_dow)
    assert schedule_from_trigger(trigger) == {"crontab": f"0 9 * * {crontab_dow}"}


def test_schedule_from_trigger():
    assert schedule_from_trigger(CronTrigger(minute="0", hour="9", day_of_week="mon-fri")) == {
        "crontab": "0 9 * * 1,2,3,4,5"
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


async def test_automation_rule_fire_checks_in(lens, monkeypatch):
    from pocketpaw.automations.evaluator import AutomationEvaluator

    seen, _ = lens
    evaluator = AutomationEvaluator()
    fired = []

    async def dispatch(rule):
        fired.append(rule.id)

    monkeypatch.setattr(evaluator, "_dispatch_rule", dispatch)
    await evaluator._fire_rule(SimpleNamespace(id="rule-9"))
    await flush()
    assert fired == ["rule-9"]
    assert [(s["body"]["monitor"], s["body"]["status"]) for s in seen] == [
        ("automation_rule:rule-9", "in_progress"),
        ("automation_rule:rule-9", "ok"),
    ]


async def test_resilient_query_is_traced(capfire, monkeypatch):
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "1")

    async def fake_query(prompt, options):
        yield "event-1"
        yield "event-2"

    fake_self = SimpleNamespace(_query=fake_query, _connect_timeout=lambda: 5.0)
    events = [e async for e in ClaudeSDKBackend._resilient_query(fake_self, "hi", None)]
    assert events == ["event-1", "event-2"]
    spans = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "invoke_agent"]
    assert len(spans) == 1
    assert spans[0]["attributes"]["pocketpaw.claude_sdk.mode"] == "stateless"
    assert spans[0]["attributes"]["gen_ai.operation.name"] == "invoke_agent"


async def test_resilient_query_finalized_in_another_task(capfire, monkeypatch, caplog):
    """Break early, close from another task: one ended span, no context detach error."""
    from opentelemetry import trace

    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    monkeypatch.setenv("POCKETPAW_LOGFIRE_ENABLED", "1")

    async def fake_query(prompt, options):
        yield "event-1"
        yield "event-2"

    fake_self = SimpleNamespace(_query=fake_query, _connect_timeout=lambda: 5.0)
    gen = ClaudeSDKBackend._resilient_query(fake_self, "hi", None)
    assert await anext(gen) == "event-1"
    assert not trace.get_current_span().is_recording(), "span leaked into the consumer"
    await asyncio.create_task(gen.aclose())
    assert not any("Failed to detach" in r.getMessage() for r in caplog.records)
    spans = [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == "invoke_agent"]
    assert len(spans) == 1 and spans[0]["end_time"]


async def test_flush_delivers_a_pending_ok(lens):
    seen, _ = lens

    async def slow(request):
        await asyncio.sleep(0.2)
        seen.append({"body": json.loads(request.content)})
        return httpx.Response(202)

    lens_checkins._transport = httpx.MockTransport(slow)
    async with automation_run("job", "slow"):
        pass
    assert lens_checkins._pending, "the ok post is still in flight when the job returns"
    assert all(s["body"]["status"] != "ok" for s in seen)
    await flush(timeout=1.0)
    assert [s["body"]["status"] for s in seen if "status" in s["body"]][-1] == "ok"
    assert not lens_checkins._pending


async def test_flush_never_raises(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(lens_checkins, "_pending", {object()})
    monkeypatch.setattr(lens_checkins.asyncio, "wait", boom)
    await flush(timeout=0.1)
