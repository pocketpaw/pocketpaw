# tests/cloud/meetings/test_meeting_schedule.py — scheduled meetings on meeting rooms.
#
# Created 2026-10-01 (feat/meetings-ics, MC-4). A dated ``POST /meetings`` with
# ``source="livekit"`` and no ``group_id`` is a meeting-room meeting: the reminder
# (5 min before) goes to the room's members, auto-start runs the call on the
# meeting's own row (budget gate, no twin row), auto-end ends the room.
# ``PATCH /meetings/{id}`` reschedules (host only, scheduled + not started, LiveKit
# only) and moves the jobs, the link expiry and the in-app calendar event.
# ``GET /meetings/{id}/joining-info`` (text/plain) and ``GET /meetings/{id}/ics``
# (RFC 5545) are open to the meeting's workspace, same as ``GET /meetings/{id}``.
# The APScheduler is real (one per test, see conftest); LiveKit is the stateful
# mock from conftest.

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pocketpaw_ee.cloud.meetings.providers.livekit  # noqa: F401,I001 — registers provider
import pytest
from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.bridges import calendar as calendar_bridge
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.meetings.scheduling import reminders
from pocketpaw_ee.cloud.models.meeting import Meeting
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.shared.events import EventBus

# Wednesday 9 January 2030, 14:00 UTC — far enough out that no job fires mid-test.
START = datetime(2030, 1, 9, 14, 0, tzinfo=UTC)


async def _user(name: str) -> str:
    user = User(email=f"{name.split()[0].lower()}@x.dev", hashed_password="x", full_name=name)
    await user.insert()
    return str(user.id)


async def _workspace(slug: str = "acme", plan: str = "enterprise") -> str:
    ws = Workspace(name="Acme", slug=slug, owner="owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _meeting(ws_id: str, host: str, **body):
    body = {"source": "livekit", "title": "Planning", **body}
    return await meetings_service.create_meeting(ws_id, host, CreateMeetingRequest(**body))


async def _dated(ws_id: str, host: str, start: datetime = START, minutes: int = 45, **body):
    return await _meeting(ws_id, host, scheduled_start=start, duration_minutes=minutes, **body)


def _job_at(kind: str, meeting_id: str) -> datetime | None:
    job = reminders._get_scheduler().get_job(f"{kind}:{meeting_id}")
    return None if job is None else job.trigger.run_date.astimezone(UTC)


def _utc(dt: datetime | None) -> datetime | None:
    return meetings_service._aware(dt)


# ---------------------------------------------------------------------------
# Scheduled meeting-room meetings + their jobs
# ---------------------------------------------------------------------------


async def test_dated_meeting_is_a_meeting_room_with_jobs_at_the_utc_instants(mongo_db, lk):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    # 19:30 in India is 14:00 UTC. The jobs must fire at the UTC instant, not
    # 19:30 UTC (the naive replace(tzinfo=UTC) bug).
    ist = START.astimezone(ZoneInfo("Asia/Kolkata"))

    out = await _dated(ws_id, host, start=ist)

    assert out.room_group_id and out.code and out.link
    assert await group_service.is_meeting_room(out.room_group_id)
    assert _job_at("reminder", out.id) == START - timedelta(minutes=5)
    assert _job_at("autostart", out.id) == START
    assert _job_at("autoend", out.id) == START + timedelta(minutes=45)


async def test_reminder_goes_to_the_meeting_rooms_members(mongo_db, lk, monkeypatch):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    mate = await _user("Mia Mate")
    out = await _dated(ws_id, host)
    await group_service.add_meeting_room_member(out.room_group_id, mate)
    created = AsyncMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.notifications.service.create", created)

    await reminders._send_reminder(await Meeting.get(out.id))

    assert sorted(c.kwargs["recipient"] for c in created.call_args_list) == sorted([host, mate])
    assert {c.kwargs["source"].room_id for c in created.call_args_list} == {out.room_group_id}
    assert {c.kwargs["kind"] for c in created.call_args_list} == {"meeting_reminder"}


async def test_auto_start_runs_the_call_on_the_meetings_own_row(mongo_db, lk, monkeypatch):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _dated(ws_id, host)
    real_create_room = livekit_service.create_room
    spy = AsyncMock(side_effect=real_create_room)
    monkeypatch.setattr(livekit_service, "create_room", spy)

    await reminders._auto_start_meeting(await Meeting.get(out.id))

    spy.assert_awaited_once()
    args, kwargs = spy.await_args
    assert args[:2] == (out.room_group_id, ws_id)  # the workspace → the budget gate
    assert kwargs.get("record_meeting") is False
    assert await Meeting.find_all().count() == 1  # no "Instant call" twin
    row = await Meeting.get(out.id)
    assert row.status == "in_progress"
    assert row.actual_start is not None
    assert row.provider_meeting_id == f"group-call-{out.room_group_id}"
    assert [r.name for r in lk.rooms] == [f"group-call-{out.room_group_id}"]


async def test_auto_start_is_refused_by_the_daily_call_budget(mongo_db, lk):
    ws_id = await _workspace(plan="free")  # free plan: no call time at all
    host = await _user("Hana Host")
    out = await _dated(ws_id, host)

    await reminders._auto_start_meeting(await Meeting.get(out.id))

    row = await Meeting.get(out.id)
    assert row.status == "failed"
    assert row.raw_provider_payload["start_error"] == "billing.call_limit"
    assert lk.rooms == []
    assert await Meeting.find_all().count() == 1


async def test_auto_end_ends_the_meeting_room(mongo_db, lk, monkeypatch):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _dated(ws_id, host)
    await reminders._auto_start_meeting(await Meeting.get(out.id))
    end_room = AsyncMock()
    monkeypatch.setattr(livekit_service, "end_room", end_room)

    await reminders._auto_end_meeting(await Meeting.get(out.id))

    end_room.assert_awaited_once_with(out.room_group_id, ws_id)
    assert (await Meeting.get(out.id)).status == "ended"


async def test_jobs_convert_an_offset_aware_start_instead_of_relabelling_it(mongo_db):
    """schedule_meeting_jobs must convert an aware start to UTC. It used to do
    replace(tzinfo=UTC), which files 09:00-05:00 as 09:00 UTC (5 hours early)."""
    new_york = START.astimezone(ZoneInfo("America/New_York"))  # 09:00-05:00
    doc = Meeting(
        workspace="w",
        join_url="",
        scheduled_start=new_york,
        scheduled_end=new_york + timedelta(minutes=45),
        raw_provider_payload={"duration_minutes": 45},
    )
    await doc.insert()
    doc.scheduled_start, doc.scheduled_end = new_york, new_york + timedelta(minutes=45)

    reminders.schedule_meeting_jobs(doc)

    assert _job_at("reminder", str(doc.id)) == START - timedelta(minutes=5)
    assert _job_at("autostart", str(doc.id)) == START
    assert _job_at("autoend", str(doc.id)) == START + timedelta(minutes=45)


# ---------------------------------------------------------------------------
# PATCH rescheduling
# ---------------------------------------------------------------------------


async def _setup(client, lk, **body):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _dated(ws_id, host, **body)
    client.act_as(host, ws_id)
    return ws_id, host, out


async def test_host_reschedules_and_everything_moves(client, lk):
    ws_id, host, out = await _setup(client, lk)
    new_start = START + timedelta(days=3)

    resp = await client.patch(
        f"/api/v1/meetings/{out.id}",
        json={"scheduled_start": "2030-01-12T19:30:00+05:30", "duration_minutes": 60},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scheduled_start"] == "2030-01-12T14:00:00Z"
    assert body["scheduled_end"] == "2030-01-12T15:00:00Z"
    assert body["duration_minutes"] == 60
    row = await Meeting.get(out.id)
    assert _utc(row.scheduled_start) == new_start
    assert _utc(row.scheduled_end) == new_start + timedelta(hours=1)
    assert _utc(row.link_expires_at) == new_start + timedelta(hours=1)
    assert row.raw_provider_payload["duration_minutes"] == 60
    assert _job_at("reminder", out.id) == new_start - timedelta(minutes=5)
    assert _job_at("autostart", out.id) == new_start
    assert _job_at("autoend", out.id) == new_start + timedelta(hours=1)


async def test_rescheduling_to_within_five_minutes_drops_the_old_reminder(client, lk):
    _, _, out = await _setup(client, lk)
    assert _job_at("reminder", out.id) is not None
    soon = (datetime.now(UTC) + timedelta(minutes=3)).replace(microsecond=0)

    resp = await client.patch(
        f"/api/v1/meetings/{out.id}", json={"scheduled_start": soon.isoformat()}
    )

    assert resp.status_code == 200, resp.text
    assert _job_at("reminder", out.id) is None  # the old reminder must not survive
    assert _job_at("autostart", out.id) == soon


async def test_duration_only_moves_the_end(client, lk):
    _, _, out = await _setup(client, lk)

    resp = await client.patch(f"/api/v1/meetings/{out.id}", json={"duration_minutes": 90})

    assert resp.status_code == 200, resp.text
    assert resp.json()["scheduled_end"] == "2030-01-09T15:30:00Z"
    row = await Meeting.get(out.id)
    assert _utc(row.scheduled_start) == START
    assert _utc(row.link_expires_at) == START + timedelta(minutes=90)
    assert _job_at("autoend", out.id) == START + timedelta(minutes=90)
    assert _job_at("autostart", out.id) == START


async def test_an_undated_meeting_gets_a_date(client, lk):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _meeting(ws_id, host)  # "for later", no date, no jobs
    assert _job_at("autostart", out.id) is None
    client.act_as(host, ws_id)

    resp = await client.patch(
        f"/api/v1/meetings/{out.id}", json={"scheduled_start": "2030-01-09T14:00:00Z"}
    )

    assert resp.status_code == 200, resp.text
    row = await Meeting.get(out.id)
    assert _utc(row.scheduled_end) == START + timedelta(minutes=30)  # default duration
    assert _utc(row.link_expires_at) == row.scheduled_end.replace(tzinfo=UTC)
    assert _job_at("autostart", out.id) == START


@pytest.mark.parametrize("status", ["in_progress", "ended", "cancelled", "failed"])
async def test_only_a_meeting_that_has_not_started_can_be_rescheduled(client, lk, status):
    _, _, out = await _setup(client, lk)
    row = await Meeting.get(out.id)
    row.status = status
    await row.save()

    resp = await client.patch(
        f"/api/v1/meetings/{out.id}", json={"scheduled_start": "2030-02-01T10:00:00Z"}
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "meeting.not_reschedulable"
    assert _utc((await Meeting.get(out.id)).scheduled_start) == START


async def test_a_started_meeting_is_not_reschedulable_even_if_still_scheduled(client, lk):
    _, _, out = await _setup(client, lk)
    row = await Meeting.get(out.id)
    row.actual_start = datetime.now(UTC)
    await row.save()

    resp = await client.patch(f"/api/v1/meetings/{out.id}", json={"duration_minutes": 10})

    assert resp.status_code == 409


async def test_a_live_meeting_can_still_be_renamed(client, lk):
    _, _, out = await _setup(client, lk)
    row = await Meeting.get(out.id)
    row.status = "in_progress"
    await row.save()

    resp = await client.patch(f"/api/v1/meetings/{out.id}", json={"title": "Retro"})

    assert resp.status_code == 200


async def test_only_the_host_can_reschedule(client, lk):
    ws_id, _, out = await _setup(client, lk)
    mate = await _user("Mia Mate")
    await group_service.add_meeting_room_member(out.room_group_id, mate)
    client.act_as(mate, ws_id)

    resp = await client.patch(f"/api/v1/meetings/{out.id}", json={"duration_minutes": 10})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "meeting.host_only"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ended"},
        {"scheduled_end": "2030-01-09T15:00:00Z"},
        {"duration_minutes": 0},
        {"duration_minutes": 1441},
    ],
)
async def test_patch_refuses_other_schedule_fields(client, lk, body):
    _, _, out = await _setup(client, lk)

    resp = await client.patch(f"/api/v1/meetings/{out.id}", json=body)

    assert resp.status_code == 422
    assert (await Meeting.get(out.id)).status == "scheduled"


async def test_a_recall_meeting_is_not_rescheduled_locally(client, lk):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    row = Meeting(
        workspace=ws_id,
        source="recall",
        provider="zoom",
        provider_meeting_id="z1",
        title="Zoom call",
        join_url="https://zoom.us/j/1",
        scheduled_start=START,
        created_by_user_id=host,
    )
    await row.insert()
    client.act_as(host, ws_id)

    resp = await client.patch(f"/api/v1/meetings/{row.id}", json={"duration_minutes": 10})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "meeting.not_reschedulable"


# ---------------------------------------------------------------------------
# Joining info
# ---------------------------------------------------------------------------


def _expected_info(out, description: str | None = None) -> str:
    lines = [
        "Planning",
        "Wednesday, 9 January 2030 · 14:00–14:45 UTC",
        "2030-01-09T14:00:00Z – 2030-01-09T14:45:00Z",
        f"Join: {out.link}",
        f"Meeting code: {out.code}",
    ]
    if description:
        lines += ["", description]
    return "\n".join(lines)


async def test_joining_info_is_plain_text_for_the_workspace(client, lk):
    ws_id, host, out = await _setup(client, lk)
    mate = await _user("Mia Mate")  # a colleague who is not in the meeting room
    client.act_as(mate, ws_id)

    resp = await client.get(f"/api/v1/meetings/{out.id}/joining-info")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/plain; charset=utf-8"
    assert resp.text == _expected_info(out)


async def test_joining_info_is_on_the_meeting_json_too(client, lk):
    _, _, out = await _setup(client, lk)
    assert out.joining_info == _expected_info(out)

    resp = await client.get(f"/api/v1/meetings/{out.id}")

    assert resp.json()["joining_info"] == _expected_info(out)


async def test_joining_info_carries_the_description(client, lk):
    _, _, out = await _setup(client, lk)
    await client.patch(f"/api/v1/meetings/{out.id}", json={"description": "Bring numbers."})

    resp = await client.get(f"/api/v1/meetings/{out.id}/joining-info")

    assert resp.text == _expected_info(out, "Bring numbers.")


async def test_joining_info_for_an_undated_meeting_has_no_date(client, lk):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _meeting(ws_id, host)
    client.act_as(host, ws_id)

    resp = await client.get(f"/api/v1/meetings/{out.id}/joining-info")

    assert resp.text == f"Planning\nJoin: {out.link}\nMeeting code: {out.code}"


async def test_joining_info_across_midnight_names_both_days(client, lk):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _dated(ws_id, host, start=START.replace(hour=23, minute=30))
    client.act_as(host, ws_id)

    resp = await client.get(f"/api/v1/meetings/{out.id}/joining-info")

    assert resp.text.splitlines()[1] == (
        "Wednesday, 9 January 2030 · 23:30 UTC – Thursday, 10 January 2030 · 00:15 UTC"
    )


@pytest.mark.parametrize("path", ["joining-info", "ics"])
async def test_another_workspace_gets_404(client, lk, path):
    _, host, out = await _setup(client, lk)
    other = await _workspace("other")
    client.act_as(host, other)

    resp = await client.get(f"/api/v1/meetings/{out.id}/{path}")

    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["joining-info", "ics"])
async def test_signed_out_gets_401(client, lk, path):
    _, _, out = await _setup(client, lk)
    client.log_out()

    resp = await client.get(f"/api/v1/meetings/{out.id}/{path}")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# .ics
# ---------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    assert text.endswith("\r\n")
    raw = text[:-2].split("\r\n")
    for line in raw:
        assert "\n" not in line and "\r" not in line
        assert len(line.encode("utf-8")) <= 75, line
    out: list[str] = []
    for line in raw:
        if line.startswith(" "):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _props(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in _unfold(text):
        name, _, value = line.partition(":")
        props.setdefault(name, value)
    return props


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\":
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            assert nxt in "nN\\;,", f"bad escape \\{nxt}"
            i += 2
            continue
        assert ch not in ";,", f"unescaped {ch!r} in {value!r}"
        out.append(ch)
        i += 1
    return "".join(out)


async def test_ics_is_one_utc_vevent(client, lk):
    _, _, out = await _setup(client, lk)

    resp = await client.get(f"/api/v1/meetings/{out.id}/ics")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/calendar; charset=utf-8"
    assert resp.headers["content-disposition"] == 'attachment; filename="planning.ics"'
    lines = _unfold(resp.text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert lines.count("BEGIN:VEVENT") == 1 and lines.count("END:VEVENT") == 1
    assert "VERSION:2.0" in lines
    assert any(line.startswith("PRODID:") for line in lines)
    props = _props(resp.text)
    assert props["UID"] == f"{out.id}@app.example.com"
    assert props["DTSTART"] == "20300109T140000Z"
    assert props["DTEND"] == "20300109T144500Z"
    assert props["DTSTAMP"].endswith("Z") and len(props["DTSTAMP"]) == 16
    assert props["SUMMARY"] == "Planning"
    assert props["URL"] == out.link
    assert props["LOCATION"] == out.link
    assert _unescape(props["DESCRIPTION"]) == _expected_info(out)
    assert "ORGANIZER" not in props  # no name-only cal-address exists; no email leaks


async def test_ics_escapes_and_folds(client, lk):
    title = "Plan; review, and \\ sync"
    description = "Line one, with; stuff\nLine two — naïve café " + "é" * 60 + " 🎉" * 10
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _dated(ws_id, host, title=title)
    client.act_as(host, ws_id)
    await client.patch(f"/api/v1/meetings/{out.id}", json={"description": description})

    resp = await client.get(f"/api/v1/meetings/{out.id}/ics")

    raw = resp.text
    assert "SUMMARY:Plan\\; review\\, and \\\\ sync" in _unfold(raw)
    for physical in raw[:-2].split("\r\n"):
        physical.encode("utf-8").decode("utf-8")  # never split inside a character
    props = _props(raw)
    assert _unescape(props["SUMMARY"]) == title
    info = (await client.get(f"/api/v1/meetings/{out.id}/joining-info")).text
    assert description in info
    assert _unescape(props["DESCRIPTION"]) == info
    assert resp.headers["content-disposition"] == 'attachment; filename="plan-review-and-sync.ics"'


@pytest.mark.parametrize(
    ("local", "utc"),
    [
        ("2030-07-10T10:00:00-04:00", "20300710T140000Z"),  # New York, summer time
        ("2030-01-09T09:00:00-05:00", "20300109T140000Z"),  # New York, winter time
    ],
)
async def test_ics_times_are_utc_whatever_the_server_zone(client, lk, local, utc):
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"  # the server's own zone must not leak in
    time.tzset()
    try:
        ws_id = await _workspace()
        host = await _user("Hana Host")
        out = await _dated(ws_id, host, start=datetime.fromisoformat(local))
        client.act_as(host, ws_id)

        props = _props((await client.get(f"/api/v1/meetings/{out.id}/ics")).text)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert props["DTSTART"] == utc


async def test_ics_for_an_undated_meeting_is_409(client, lk):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _meeting(ws_id, host)
    client.act_as(host, ws_id)

    resp = await client.get(f"/api/v1/meetings/{out.id}/ics")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "meeting.not_scheduled"


# ---------------------------------------------------------------------------
# In-app calendar event (reverse bridge)
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar(monkeypatch):
    """The meetings bus wired to the calendar bridge; calendar.service faked."""
    from types import SimpleNamespace

    bus = EventBus()
    bus.subscribe("meeting.scheduled", calendar_bridge._on_meeting_scheduled)
    bus.subscribe("meeting.cancelled", calendar_bridge._on_meeting_cancelled)
    bus.subscribe("meeting.edited", calendar_bridge._on_meeting_edited)
    monkeypatch.setattr(meetings_service, "event_bus", bus)

    def _made(ctx, body):
        return SimpleNamespace(id="evt-1")

    fakes = SimpleNamespace(
        create=AsyncMock(side_effect=_made), update=AsyncMock(), delete=AsyncMock()
    )
    monkeypatch.setattr("pocketpaw_ee.calendar.service.create_event", fakes.create)
    monkeypatch.setattr("pocketpaw_ee.calendar.service.update_event", fakes.update)
    monkeypatch.setattr("pocketpaw_ee.calendar.service.delete_event", fakes.delete)
    return fakes


async def test_calendar_event_carries_the_link_and_joining_info(client, lk, calendar):
    _, host, out = await _setup(client, lk)

    calendar.create.assert_awaited_once()
    ctx, body = calendar.create.await_args.args
    assert ctx.user_id == host
    assert body.location == out.link
    assert body.description == _expected_info(out)
    assert body.starts_at == START
    assert (await Meeting.get(out.id)).raw_provider_payload["calendar_event_id"] == "evt-1"


async def test_rescheduling_moves_the_calendar_event(client, lk, calendar):
    _, host, out = await _setup(client, lk)

    resp = await client.patch(
        f"/api/v1/meetings/{out.id}",
        json={"scheduled_start": "2030-01-12T14:00:00Z", "duration_minutes": 60},
    )

    assert resp.status_code == 200
    calendar.update.assert_awaited_once()
    ctx, event_id, body = calendar.update.await_args.args
    assert (ctx.user_id, event_id) == (host, "evt-1")
    assert body.starts_at == datetime(2030, 1, 12, 14, 0, tzinfo=UTC)
    assert body.ends_at == datetime(2030, 1, 12, 15, 0, tzinfo=UTC)
    assert body.location == out.link
    assert "Saturday, 12 January 2030 · 14:00–15:00 UTC" in body.description
    assert calendar.create.await_count == 1  # moved, not duplicated


async def test_retitling_updates_the_calendar_event(client, lk, calendar):
    _, _, out = await _setup(client, lk)

    await client.patch(f"/api/v1/meetings/{out.id}", json={"title": "Retro"})

    body = calendar.update.await_args.args[2]
    assert body.title == "Retro"
    assert body.description.startswith("Retro\n")


async def test_dating_an_undated_meeting_adds_it_to_the_calendar(client, lk, calendar):
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await _meeting(ws_id, host)
    calendar.create.assert_not_awaited()  # nothing to put on a calendar yet
    client.act_as(host, ws_id)

    await client.patch(
        f"/api/v1/meetings/{out.id}", json={"scheduled_start": "2030-01-09T14:00:00Z"}
    )

    calendar.create.assert_awaited_once()
    calendar.update.assert_not_awaited()
    assert calendar.create.await_args.args[1].location == out.link


async def test_cancelling_removes_the_calendar_event(client, lk, calendar):
    _, host, out = await _setup(client, lk)

    resp = await client.delete(f"/api/v1/meetings/{out.id}")

    assert resp.status_code == 200
    calendar.delete.assert_awaited_once()
    ctx, event_id = calendar.delete.await_args.args
    assert (ctx.user_id, event_id) == (host, "evt-1")
    assert _job_at("autostart", out.id) is None
